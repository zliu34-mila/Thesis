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

from __future__ import annotations

import argparse
import json
import re
import time

from emotion_map import EMOTIONS, emotion_to_gestures

MODES = ["still", "breathe", "wave", "ripple", "shiver", "reach", "heartbeat", "scan"]

# 速度 → 一个周期的毫秒数
SPEED_TO_PERIOD = {"very_slow": 5000, "slow": 3000, "medium": 1600, "fast": 800, "very_fast": 400}

# 结构化输出：让 Ollama 严格按这个格式回答。
# 字段顺序很重要：先判断类型，再用一句话写下理解（intent），然后才给情绪打分、选动作，相当于“先想后做”。
SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["command", "emotion", "scene", "conversation"]},
        "intent": {"type": "string"},
        "affect": {
            "type": "object",
            "properties": {
                "cues": {"type": "array", "items": {"type": "string"}},
                "emotion": {"type": "string", "enum": EMOTIONS},
                "secondary": {"type": "string", "enum": EMOTIONS + ["none"]},
                "valence": {"type": "number"},
                "arousal": {"type": "number"},
                "intensity": {"type": "number"},
            },
            "required": ["cues", "emotion", "secondary", "valence", "arousal", "intensity"],
        },
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
    "required": ["type", "intent", "affect", "gestures", "reply"],
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


def decide_gestures(plan: dict) -> tuple[list[dict], str]:
    """决定最终用哪些动作。
    情绪类的话：用情绪引擎（emotion_map.py）按规则计算，结果稳定、可解释。
    指令、场景、对话：直接用模型给出的动作（模型擅长把“风吹过”对应成波浪）。
    """
    if plan.get("type") == "emotion" and isinstance(plan.get("affect"), dict):
        return emotion_to_gestures(plan["affect"]), "emotion engine"
    return (plan.get("gestures") or [{"mode": "still"}]), "model"


def plan_to_commands(plan: dict) -> list[tuple[str, float]]:
    gestures, _ = decide_gestures(plan)
    return [to_command(g) for g in gestures[:3]]


def describe_affect(plan: dict) -> str:
    a = plan.get("affect") or {}
    sec = a.get("secondary")
    sec = f" + {sec}" if sec and sec != "none" else ""
    try:
        nums = f"valence {float(a.get('valence', 0)):+.2f}  arousal {float(a.get('arousal', 0)):.2f}  intensity {float(a.get('intensity', 0)):.2f}"
    except (TypeError, ValueError):
        nums = ""
    return f"{a.get('emotion', '?')}{sec}  {nums}  cues: {', '.join(a.get('cues', [])[:5])}"


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


# ================= 对话节奏：思考 → I FEEL YOU → Reply =================
# 模型思考期间，舵机循环播放这个“思考”动作，直到结果出来后被新动作打断。
# 想换一种思考的样子，改这里就行（mode 可选 still breathe wave ripple shiver reach heartbeat scan）。
THINKING_GESTURE = {"mode": "scan", "amplitude": 30, "speed": "slow", "direction": "right"}

# 情绪动作做完之后，停顿几秒再显示 Reply（舵机这时回到中间静止）。想间隔更久就把数字调大。
REPLY_PAUSE_S = 3.0


def thinking_command() -> str:
    cmd, _ = to_command({**THINKING_GESTURE, "duration_s": 1})
    parts = cmd.split()
    parts[4] = "0"                               # 时长 0 = 一直循环
    return " ".join(parts)


def shows_feeling(plan: dict) -> bool:
    """只要读出了情绪（不是 neutral），而且不是纯指令，就显示 I FEEL YOU。
    这样即使模型把一句话归成 conversation 或 scene，情绪分析也不会被藏起来。"""
    emotion = (plan.get("affect") or {}).get("emotion")
    return plan.get("type") != "command" and emotion not in (None, "", "neutral")


def feel_phrase(affect: dict) -> str:
    """把情绪分析变成一句话，例如 "so much joy, with a touch of contentment" """
    emotion = affect.get("emotion", "neutral")
    word = "a quiet mood" if emotion == "neutral" else emotion
    try:
        k = float(affect.get("intensity", 0.5))
    except (TypeError, ValueError):
        k = 0.5
    if emotion != "neutral":
        if k < 0.35:
            word = f"a little {word}"
        elif k >= 0.9:
            word = f"overwhelming {word}"
        elif k >= 0.65:
            word = f"so much {word}"
    sec = affect.get("secondary")
    if sec and sec not in ("none", "neutral", emotion):
        word += f", with a touch of {sec}"
    return word


# ================= 确定性的小规则：补上模型不稳定的地方 =================
# 面试发现：模型说 "a little sad" 时强度仍然打 0.6；说 "Bigger!" 时不一定能准确放大上一个动作。
# 这两件事规则很明确，用代码来做比让模型猜更可靠。

SOFTENERS = r"\b(a little|a bit|a tiny bit|kind of|kinda|sort of|slightly|somewhat|i guess)\b"
INTENSIFIERS = r"\b(very|really|so|extremely|super|totally|incredibly)\b|!!"


def adjust_intensity(text: str, plan: dict) -> None:
    """有 “a little / kind of” 这类减弱词、又没有 “very / so” 这类加强词时，强度最高 0.35。"""
    a = plan.get("affect")
    low = text.lower()
    if isinstance(a, dict) and re.search(SOFTENERS, low) and not re.search(INTENSIFIERS, low):
        try:
            a["intensity"] = min(float(a.get("intensity", 0.5)), 0.35)
        except (TypeError, ValueError):
            a["intensity"] = 0.3


# 追加指令：在“上一次实际做过的动作”上修改
MODIFIERS = [
    (r"\b(bigger|larger|stronger|more)\b", "bigger"),
    (r"\b(smaller|softer|gentler|less|weaker)\b", "smaller"),
    (r"\b(faster|quicker|speed up)\b", "faster"),
    (r"\b(slower|slow down)\b", "slower"),
    (r"\b(other way|opposite|reverse|other direction)\b", "flip"),
    (r"\b(longer)\b", "longer"),
    (r"\b(shorter)\b", "shorter"),
    (r"\b(again|repeat|one more time|once more)\b", "again"),
]
SPEED_ORDER = list(SPEED_TO_PERIOD)          # very_slow ... very_fast


def apply_modifiers(text: str, last: list) -> list | None:
    """短句里出现 bigger / faster / again 等词，就在上一组动作上调整；否则返回 None。"""
    if not last or len(text.split()) > 6:
        return None
    low = text.lower()
    found = [name for pattern, name in MODIFIERS if re.search(pattern, low)]
    if not found:
        return None
    out = []
    for g in last:
        if g.get("mode") == "still":
            continue
        g = dict(g)
        idx = SPEED_ORDER.index(g["speed"]) if g.get("speed") in SPEED_ORDER else 2
        for f in found:
            if f == "bigger":
                g["amplitude"] = min(100, round(g.get("amplitude", 50) * 1.4) + 5)
            elif f == "smaller":
                g["amplitude"] = max(15, round(g.get("amplitude", 50) * 0.6))
            elif f == "faster":
                idx = min(4, idx + 1)
            elif f == "slower":
                idx = max(0, idx - 1)
            elif f == "flip":
                g["direction"] = "left" if g.get("direction") == "right" else "right"
            elif f == "longer":
                g["duration_s"] = min(15, round(float(g.get("duration_s", 5)) * 1.5, 1))
            elif f == "shorter":
                g["duration_s"] = max(1, round(float(g.get("duration_s", 5)) * 0.6, 1))
        g["speed"] = SPEED_ORDER[idx]
        out.append(g)
    return out or None


def last_gestures(history: list) -> list:
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            try:
                return json.loads(msg["content"]).get("gestures") or []
            except (ValueError, TypeError):
                return []
    return []


# ================= 模型 =================
def ask_model(model: str, history: list, text: str) -> dict:
    import ollama
    resp = ollama.chat(
        model=model,
        messages=history + [{"role": "user", "content": text}],
        format=SCHEMA,
    )
    plan = json.loads(resp.message.content)
    adjust_intensity(text, plan)
    if plan.get("type") == "command":
        modified = apply_modifiers(text, last_gestures(history))
        if modified:
            plan["gestures"] = modified
    # 保留最近 3 轮对话，这样可以说 "bigger" "the other way"。
    # 记忆里存的是舵机“实际做出的动作”（情绪句子由情绪引擎计算），
    # 否则说 "Bigger!" 时，模型会去放大一个其实没做过的动作。
    remembered = dict(plan)
    remembered["gestures"], _ = decide_gestures(plan)
    history += [{"role": "user", "content": text},
                {"role": "assistant", "content": json.dumps(remembered, ensure_ascii=False)}]
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
    ap.add_argument("--debug", action="store_true", help="显示完整分析过程（类型、情绪数值、串口指令）")
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

        # 第 1 步：思考。舵机缓慢循环“思考”动作，同时模型在分析
        link.send(thinking_command())
        print(" Sema is thinking...")
        try:
            plan = ask_model(args.model, history, text)
        except Exception as e:
            print(f"  Model error: {e}")
            link.send("H")
            link.wait_done(5)
            continue

        commands = plan_to_commands(plan)
        if args.debug:
            _, source = decide_gestures(plan)
            print(f"  [type]    {plan.get('type', '?')}")
            print(f"  [affect]  {describe_affect(plan)}")
            print(f"  [intent]  {plan.get('intent', '')}")
            for cmd, _ in commands:
                print(f"  [action]  {cmd}   ({source})")

        # 第 2 步：I FEEL YOU。显示读到的情绪，同时舵机做出对应的动作
        if shows_feeling(plan):
            print(f" I Think You: {feel_phrase(plan.get('affect') or {})}")
        play(commands, link)
        if shows_feeling(plan):
            time.sleep(REPLY_PAUSE_S)

        # 第 3 步：回应
        print(f" Sema:   {plan.get('reply', '')}")

if __name__ == "__main__":
    main()