"""
面试 semaform：用一组设计好的对话测试她的理解能力，把完整回答记录到 interview_log.txt。
不需要连接舵机。运行：python interview.py   （或双击 6_interview.bat）

每组对话是连续的多轮（她会记得前面说过的话），和真实使用时一样。
"""

from __future__ import annotations

import json
import sys
import time

from agent import ask_model, decide_gestures, plan_to_commands, shows_feeling, feel_phrase, describe_affect

MODEL = sys.argv[1] if len(sys.argv) > 1 else "semaform"

# 每一项：(这组对话在测什么, [连续的几句话])
CONVERSATIONS = [
    ("Long happy story, then a feeling shift, then a command follow-up", [
        "Hey, I ate a very delicious food, and I feel the weather is also good! I really enjoy the moment that I stay with my friends on the beach today!",
        "But I'm a little sad that the day is almost over.",
        "Bigger!",
    ]),
    ("Mixed feelings in one sentence", [
        "I got into my dream school, but I have to move far away from my family.",
    ]),
    ("Sarcasm: positive words, negative meaning", [
        "Oh great, it's raining again. Just perfect.",
    ]),
    ("Subtle, low-energy sadness", [
        "I don't know. Everything feels kind of grey lately.",
    ]),
    ("Negative story that turns positive", [
        "The meeting went terribly and my boss yelled at me, but then my friend brought me coffee and made me laugh.",
    ]),
    ("Very short inputs", [
        "ugh",
        "yay!",
        "...",
    ]),
    ("Questions about Semaform itself", [
        "Do you have feelings?",
        "What do you feel right now?",
    ]),
    ("Commands with style words", [
        "Do something slow and gentle.",
        "Now go wild!",
        "Stop.",
    ]),
    ("Poetic image", [
        "The city lights look like fallen stars tonight.",
    ]),
    ("Explicitly named emotions", [
        "Show me how I feel right now: nervous but excited.",
    ]),
    ("Anger and unfairness", [
        "My roommate ate my food again and lied about it. I'm so done.",
    ]),
    ("Gratitude and love", [
        "My grandma called just to say she's proud of me.",
    ]),
]


def main():
    lines = []
    records = []

    def out(s=""):
        print(s)
        lines.append(s)

    out(f"Semaform interview   model={MODEL}   {time.strftime('%Y-%m-%d %H:%M')}")
    total_turns = sum(len(t) for _, t in CONVERSATIONS)
    n = 0
    for topic, turns in CONVERSATIONS:
        out("")
        out("=" * 70)
        out(f"# {topic}")
        history: list = []
        for text in turns:
            n += 1
            print(f"\n[{n}/{total_turns}] thinking...", flush=True)
            t0 = time.time()
            try:
                plan = ask_model(MODEL, history, text)
                err = None
            except Exception as e:
                plan, err = {}, f"{type(e).__name__}: {e}"
            dt = time.time() - t0

            out(f"\nYou > {text}")
            if err:
                out(f"  !! ERROR: {err}")
                records.append({"topic": topic, "input": text, "error": err, "seconds": round(dt, 1)})
                continue
            gestures, source = decide_gestures(plan)
            out(f"  [type]    {plan.get('type', '?')}")
            out(f"  [affect]  {describe_affect(plan)}")
            out(f"  [intent]  {plan.get('intent', '')}")
            if shows_feeling(plan):
                out(f"  I FEEL YOU : {feel_phrase(plan.get('affect') or {})}")
            for cmd, _ in plan_to_commands(plan):
                out(f"  [action]  {cmd}   ({source})")
            out(f"  Reply:   {plan.get('reply', '')}")
            out(f"  ({dt:.1f}s)")
            records.append({"topic": topic, "input": text, "plan": plan,
                            "final_gestures": gestures, "source": source, "seconds": round(dt, 1)})

    with open("interview_log.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open("interview_log.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print("\nSaved interview_log.txt and interview_log.json. Tell Claude it's done!")


if __name__ == "__main__":
    main()
