"""
Semaform 的情绪引擎：情绪分析 → 动作

分工：
  - 模型（Ollama）只负责“读”：这句话是什么情绪、有多正面（valence）、多激动（arousal）、多强烈（intensity）
  - 这个文件负责“算”：根据固定规则，把情绪换算成动作和参数
这样同一种情绪永远得到同一类动作，结果稳定、可解释，也方便你按自己的设计去调整。

理论基础：Russell 的情绪环形模型（circumplex model of affect）
  valence   情绪效价 -1（很负面）~ +1（很正面）
  arousal   唤醒度    0（很平静、很困）~ 1（很激动）
  intensity 强烈程度  0（一点点）~ 1（非常强烈）

换算规则：
  intensity → 幅度      越强烈，摆得越大
  arousal   → 速度      越激动，越快
  arousal   → 持续时间  越平静，持续越久
  emotion   → 动作      查下面的 EMOTION_MAP 表
"""

from __future__ import annotations

# 情绪 → 动作模板：(动作, 幅度系数, 速度偏移)
#   幅度系数：在由 intensity 算出的幅度上再乘一个系数
#   速度偏移：在由 arousal 算出的速度档位上再加减几档（-2 ~ +2）
# 想改变 semaform 的“性格”，就改这张表。
EMOTION_MAP = {
    #  情绪           动作模板                                         设计理由
    "joy":         [("wave", 1.0, 0)],                                # 开心：向外舒展、流动
    "contentment": [("breathe", 0.8, -1)],                            # 满足、惬意：放松地慢慢起伏
    "excitement":  [("ripple", 1.0, +1), ("shiver", 0.6, +1)],        # 兴奋：一下子炸开，然后激动得发抖
    "love":        [("heartbeat", 0.9, -1)],                          # 爱、心动：心跳
    "gratitude":   [("reach", 0.7, -1), ("breathe", 0.6, -1)],        # 感激：倾向对方，然后安定下来
    "calm":        [("breathe", 0.6, -1)],                            # 平静：慢呼吸
    "tiredness":   [("breathe", 0.5, -2)],                            # 疲惫：很慢、很小的呼吸
    "sadness":     [("reach", 0.7, -2)],                              # 难过：慢慢垂向一侧
    "nostalgia":   [("heartbeat", 0.7, -1), ("reach", 0.6, -2)],      # 怀念：轻轻的心跳，然后望向远处
    "fear":        [("shiver", 0.8, +1), ("still", 0.0, 0)],          # 害怕：发抖，然后僵住
    "nervousness": [("shiver", 0.6, +1), ("heartbeat", 0.6, 0)],      # 紧张：抖动 + 心跳加速
    "anger":       [("ripple", 1.0, +2), ("shiver", 0.9, +2)],        # 生气：猛烈爆发，然后气得发抖
    "surprise":    [("ripple", 0.9, +1)],                             # 惊讶：从中间炸开
    "curiosity":   [("scan", 0.8, 0)],                                # 好奇、思考：一个一个地看
    "neutral":     [("breathe", 0.4, 0)],                             # 没什么情绪：轻轻呼吸
}
EMOTIONS = list(EMOTION_MAP)

SPEEDS = ["very_slow", "slow", "medium", "fast", "very_fast"]

# 情绪动作的持续时间范围（秒）：最激动的情绪用最短时间，最平静的用最长时间。
# 想让情绪动作做得更久，把这两个数调大，比如 (6, 14)。
DURATION_MIN_S, DURATION_MAX_S = 4, 10


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def arousal_to_speed_index(arousal: float) -> int:
    """唤醒度 0~1 → 速度档位 0~4（very_slow ~ very_fast）"""
    return min(4, int(_clamp(arousal, 0, 1) * 5))


def emotion_to_gestures(affect: dict) -> list[dict]:
    """把情绪分析结果换算成 1~3 个动作。"""
    emotion = affect.get("emotion") if affect.get("emotion") in EMOTION_MAP else "neutral"
    secondary = affect.get("secondary")
    arousal = _clamp(affect.get("arousal", 0.4), 0, 1)
    intensity = _clamp(affect.get("intensity", 0.5), 0, 1)

    base_amp = 25 + 65 * intensity                 # 25 ~ 90
    base_speed = arousal_to_speed_index(arousal)
    duration = round(DURATION_MIN_S + (1 - arousal) * (DURATION_MAX_S - DURATION_MIN_S), 1)

    def build(template, scale=1.0):
        out = []
        for mode, amp_k, speed_shift in template:
            speed = SPEEDS[max(0, min(4, base_speed + speed_shift))]
            out.append({
                "mode": mode,
                "amplitude": 0 if mode == "still" else max(15, round(base_amp * amp_k * scale)),
                "speed": speed,
                "duration_s": 1 if mode == "still" else duration,
                "direction": "right",
            })
        return out

    gestures = build(EMOTION_MAP[emotion])

    # 次要情绪：把它的第一个动作接在后面（幅度减弱），让混合情绪有层次
    if secondary in EMOTION_MAP and secondary not in (emotion, "neutral") and len(gestures) < 3:
        extra = build(EMOTION_MAP[secondary][:1], scale=0.6)[0]
        if extra["mode"] not in [g["mode"] for g in gestures]:
            gestures.append(extra)

    return gestures[:3]


if __name__ == "__main__":
    # 直接运行这个文件，可以看到每种情绪会被换算成什么动作
    for emo in EMOTIONS:
        gs = emotion_to_gestures({"emotion": emo, "arousal": 0.5, "intensity": 0.6})
        print(f"{emo:<12}", " + ".join(f"{g['mode']}({g['amplitude']},{g['speed']})" for g in gs))