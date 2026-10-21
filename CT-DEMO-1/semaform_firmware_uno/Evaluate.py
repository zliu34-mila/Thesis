"""
给 semaform 模型打分：用 test_cases.csv 里的句子逐条提问，检查它选的动作对不对。

用法：
    python evaluate.py                    # 每句问 1 次
    python evaluate.py --runs 3           # 每句问 3 次，看回答稳不稳定
    python evaluate.py --model qwen2.5:7b # 换一个模型对比

每次改完 Modelfile、重新 ollama create 之后跑一遍，对比正确率有没有提高。
结果会另存到 results/ 文件夹，方便和上一次对比。

判断标准：模型给出的第一个动作，出现在 expected 列里（多个可接受的答案用 | 分隔）就算对。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict

from agent import SCHEMA, decide_gestures, describe_affect   # 和 agent.py 用同一套格式和决策逻辑


def ask(model: str, sentence: str) -> dict:
    import ollama
    resp = ollama.chat(model=model,
                       messages=[{"role": "user", "content": sentence}],
                       format=SCHEMA)
    return json.loads(resp.message.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="semaform")
    ap.add_argument("--cases", default="test_cases.csv")
    ap.add_argument("--runs", type=int, default=1, help="每句重复问几次")
    args = ap.parse_args()

    # 先确认 Ollama 在运行、模型存在，免得每一句都报同样的错
    try:
        import ollama
        ollama.show(args.model)
    except Exception as e:
        print(f"连不上模型 {args.model}：{e}")
        print("检查：Ollama 是否已打开？是否运行过 ollama create semaform -f Modelfile？")
        return

    with open(args.cases, encoding="utf-8-sig") as f:
        cases = list(csv.DictReader(f))

    rows = []
    by_cat = defaultdict(lambda: [0, 0])          # 类别 -> [答对次数, 总次数]
    t0 = time.time()

    for case in cases:
        expected = [m.strip() for m in case["expected"].split("|")]
        for run in range(args.runs):
            try:
                plan = ask(args.model, case["sentence"])
                gestures, _ = decide_gestures(plan)          # 和 agent.py 完全一样的决策
                modes = [g.get("mode", "?") for g in gestures]
                intent = plan.get("intent", "")
                analysis = f"[{plan.get('type', '?')}] {describe_affect(plan)}"
            except Exception as e:
                modes, intent, analysis = [], f"出错：{e}", ""
            ok = bool(modes) and modes[0] in expected
            by_cat[case["category"]][0] += ok
            by_cat[case["category"]][1] += 1
            rows.append({"category": case["category"], "sentence": case["sentence"],
                         "expected": case["expected"], "got": "+".join(modes) or "-",
                         "ok": "✓" if ok else "✗", "intent": intent, "analysis": analysis})
            print(f"{'✓' if ok else '✗'}  {case['sentence']:<22} 选了 {('+'.join(modes) or '-'):<22}"
                  f" 期望 {case['expected']:<16} {'' if ok else '意图：' + intent}")
            if not ok and analysis:
                print(f"     分析：{analysis}")

    total_ok = sum(v[0] for v in by_cat.values())
    total = sum(v[1] for v in by_cat.values())
    print("\n==== 各类别正确率 ====")
    for cat, (k, n) in by_cat.items():
        print(f"  {cat:<8} {k}/{n}  {100 * k / n:.0f}%")
    print(f"  总计     {total_ok}/{total}  {100 * total_ok / total:.0f}%   （用时 {time.time() - t0:.0f} 秒）")

    os.makedirs("results", exist_ok=True)
    out = os.path.join("results", f"{time.strftime('%m%d-%H%M')}_{args.model.replace(':', '-')}.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n详细结果已保存到 {out}")


if __name__ == "__main__":
    main()