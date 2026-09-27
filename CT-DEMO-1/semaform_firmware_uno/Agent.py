"""
Semaform agent：你说的话 → Ollama 提取意图 → 选动作 → ESP32 播放

准备：
    pip install ollama pyserial
    ollama pull qwen2.5:7b
    ollama create semaform -f Modelfile

运行：
    python agent.py --demo --port COM3          # 不用模型，依次播放全部 8 个动作（检查硬件）
    python agent.py --port COM3                 # 正式运行
    python agent.py --dry-run                   # 不接硬件，只看模型输出和指令
  Mac 的端口一般是 /dev/cu.usbserial-xxxx，Linux 是 /dev/ttyUSB0

对话用英文，例如 "I'm so tired"、"A breeze passes by"、"Hello"。
运行中输入 quit 退出，输入 stop 立即停止并回到中间。
"""

import argparse
import json
import time

MODES = ["still", "breathe", "wave", "ripple", "shiver", "reach", "heartbeat", "scan"]

# 速度 → 一个周期的毫秒数
SPEED_TO_PERIOD = {"very_slow": 5000, "slow": 3000, "medium": 1600, "fast": 800, "very_fast": 400}

# 结构化输出：让 Ollama 严格按这个格式回答
SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "gestures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": MODES},
                    "amplitude": {"type": "integer"},
                    "speed": {"type": "string", "enum": list(SPEED_TO_PERIOD)},
                    "duration_s": {"type": "number"},
                    "direction": {"type": "string", "enum": ["left", "right"]},
                },
                "required": ["mode", "amplitude", "speed", "duration_s", "direction"],
            },
        },
        "reply": {"type": "string"},
    },
    "required": ["intent", "gestures", "reply"],
}


# ================= 把模型输出转成安全的硬件指令 =================
def to_command(g: dict) -> tuple[str, float]:
    """一个 gesture → (串口指令, 持续秒数)。所有数值都裁剪到安全范围。"""
    mode = g.get("mode") if g.get("mode") in MODES else "still"
    amp = max(0, min(100, int(g.get("amplitude", 50))))
    period = SPEED_TO_PERIOD.get(g.get("speed"), 1600)
    if mode == "shiver":
        period = max(150, period // 4)          # 抖动需要更高的频率
    dur = max(1.0, min(15.0, float(g.get("duration_s", 5))))
    if mode == "still":
        dur = 1.0                                # 静止只需要回到中间，不用等
    direction = -1 if g.get("direction") == "left" else 1
    return f"M {mode} {amp} {period} {int(dur * 1000)} {direction}", dur


def plan_to_commands(plan: dict) -> list[tuple[str, float]]:
    gestures = plan.get("gestures") or [{"mode": "still"}]
    return [to_command(g) for g in gestures[:3]]


# ================= 串口 =================
class ServoLink:
    def __init__(self, port: str, baud: int = 115200):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(2)                            # 打开串口时 ESP32 会重启
        self.ser.reset_input_buffer()

    def _readline(self, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="ignore").strip()
            if line:
                return line
        return None

    def send(self, cmd: str) -> None:
        self.ser.reset_input_buffer()            # 丢掉上一个动作残留的 DONE
        self.ser.write((cmd + "\n").encode())
        reply = self._readline(1.0)
        if reply != "OK":
            raise RuntimeError(f"Unexpected firmware reply: {cmd!r} -> {reply!r}")

    def wait_done(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._readline(deadline - time.time()) == "DONE":
                return
        print("  (timed out waiting for DONE)")


class DryRunLink:
    def send(self, cmd: str) -> None:
        print(f"  -> {cmd}")

    def wait_done(self, timeout: float) -> None:
        pass


def play(commands, link) -> None:
    for cmd, dur in commands:
        link.send(cmd)
        link.wait_done(timeout=dur + 5)         # 动作时长 + 回到中间的时间


# ================= 模型 =================
def ask_model(model: str, history: list, text: str) -> dict:
    import ollama
    resp = ollama.chat(
        model=model,
        messages=history + [{"role": "user", "content": text}],
        format=SCHEMA,
    )
    plan = json.loads(resp.message.content)
    # 保留最近 3 轮对话，这样可以说 "bigger" "the other way"
    history += [{"role": "user", "content": text},
                {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}]
    del history[:-6]
    return plan


def run_demo(link) -> None:
    for mode in MODES[1:]:
        print(f"  {mode}")
        play([to_command({"mode": mode, "amplitude": 60, "speed": "medium",
                          "duration_s": 4, "direction": "right"})], link)
    print("  Demo finished")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="ESP32 串口，例如 COM3 或 /dev/ttyUSB0")
    ap.add_argument("--model", default="semaform", help="Ollama 模型名（默认 semaform）")
    ap.add_argument("--dry-run", action="store_true", help="不连接硬件")
    ap.add_argument("--demo", action="store_true", help="依次播放全部动作，不调用模型")
    args = ap.parse_args()

    link = DryRunLink() if (args.dry_run or not args.port) else ServoLink(args.port)
    if isinstance(link, DryRunLink):
        print("[dry-run] No hardware connected; printing commands only")

    if args.demo:
        run_demo(link)
        return

    history: list = []
    while True:
        try:
            text = input("\nYou > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "quit":
            break
        if text == "stop":
            link.send("H")
            link.wait_done(5)
            continue

        try:
            plan = ask_model(args.model, history, text)
        except Exception as e:
            print(f"  Model error: {e}")
            continue

        print(f"  Intent: {plan.get('intent', '')}")
        print(f"  Reply:  {plan.get('reply', '')}")
        commands = plan_to_commands(plan)
        for cmd, _ in commands:
            print(f"  Action: {cmd}")
        play(commands, link)


if __name__ == "__main__":
    main()
