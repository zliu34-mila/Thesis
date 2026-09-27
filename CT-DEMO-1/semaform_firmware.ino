/*
 * Semaform 固件 —— ESP32 + 4 × SG92R
 *
 * 核心思路（和 neoFORM 的经验一致）：动作库放在 ESP32 上，由 ESP32 实时生成动作。
 * 大模型只负责“选哪个动作、用什么参数”，不用逐帧控制舵机，所以不会被模型的延迟拖慢。
 *
 * 依赖库：ESP32Servo（Arduino 库管理器搜索 "ESP32Servo"）
 * 开发板：ESP32 Dev Module，串口 115200
 *
 * ---------------- 串口指令（每条一行）----------------
 *   M <mode> [amp] [period_ms] [dur_ms] [dir]   播放一个动作
 *        mode      : still breathe wave ripple shiver reach heartbeat scan
 *        amp       : 幅度 0~100（默认 50）
 *        period_ms : 一个周期的毫秒数，越小越快，150~20000（默认 2000）
 *        dur_ms    : 持续时间，0 = 一直播放（默认 5000）
 *        dir       : 方向 1 或 -1（默认 1）
 *      例：M wave 60 1500 8000 1
 *   P a0 a1 a2 a3   直接设定角度（调试、标定用）
 *   H               停止并回到中间
 *   ?               查询当前状态
 *   L               列出所有动作
 * 回复：OK / ERR ... ；动作结束并回到中间后回复 DONE
 */

#include <ESP32Servo.h>
#include <math.h>

// ================= 硬件配置 =================
// 舵机信号线接的 GPIO，按从左到右的顺序。只用 3 个就删掉一个。
const int SERVO_PINS[] = {13, 14, 27, 26};
constexpr int NUM = sizeof(SERVO_PINS) / sizeof(SERVO_PINS[0]);

// 每个舵机的中心微调（度）：装上摆臂后，如果 90° 时不正，就在这里修正
int centerTrim[8] = {0, 0, 0, 0, 0, 0, 0, 0};
static_assert(NUM <= 8, "最多 8 个舵机");

constexpr int   CENTER     = 90;
constexpr int   ANGLE_MIN  = 10;       // 软件限位，避免顶到机械极限
constexpr int   ANGLE_MAX  = 170;
constexpr float MAX_SWING  = 70.0f;    // amp=100 时，最大偏离中心 ±70°
constexpr float MAX_STEP   = 8.0f;     // 每个周期最多转 8°（= 400°/s），保护齿轮
constexpr uint32_t TICK_MS = 20;       // 50 Hz
constexpr uint32_t FADE_MS = 400;      // 动作开始/结束时的淡入淡出

// ================= 动作库 =================
enum Mode { STILL, BREATHE, WAVE, RIPPLE, SHIVER, REACH, HEARTBEAT, SCAN, MODE_COUNT };
const char *MODE_NAMES[MODE_COUNT] = {
  "still", "breathe", "wave", "ripple", "shiver", "reach", "heartbeat", "scan"
};

struct Gesture {
  Mode     mode     = STILL;
  float    amp      = 0;       // 0~1
  uint32_t periodMs = 2000;
  uint32_t durMs    = 0;       // 0 = 一直播放
  int      dir      = 1;
  uint32_t startMs  = 0;
  bool     active   = false;
};

Gesture g;
Servo servos[NUM];
float pos[NUM];               // 当前角度
float manualTarget[NUM];      // P 指令的目标
bool  manual      = false;
bool  pendingDone = false;    // 动作结束、回到中间后要回复 DONE
uint32_t lastTick = 0;

static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
static float smooth01(float s) { s = clampf(s, 0, 1); return s * s * (3 - 2 * s); }

// 舵机 i 在时刻 t（动作开始后的毫秒数）的偏移量，范围大约 -1 ~ 1
float patternOffset(int i, uint32_t t) {
  const float TWO_PI_F = 6.2831853f;
  float phase = (float)t / (float)g.periodMs;                 // 已经走过的周期数
  float x = (NUM > 1) ? (float)i / (float)(NUM - 1) : 0.0f;   // 在这一排里的位置 0~1
  if (g.dir < 0) x = 1.0f - x;                                // 方向反过来

  switch (g.mode) {
    case BREATHE:    // 全体同步、缓慢起伏
      return sinf(TWO_PI_F * phase);

    case WAVE:       // 从一侧传到另一侧
      return sinf(TWO_PI_F * (phase - 0.75f * x));

    case RIPPLE: {   // 从中间向两边扩散
      float d = fabsf(x - 0.5f) * 2.0f;                       // 0 = 中间，1 = 两端
      return sinf(TWO_PI_F * (phase - 0.5f * d));
    }

    case SHIVER:     // 快速小幅抖动：每个舵机用不同的频率叠加，看起来不规则
      return 0.6f * sinf(TWO_PI_F * phase * (1.0f + 0.37f * i) + i)
           + 0.4f * sinf(TWO_PI_F * phase * (2.3f + 0.21f * i) + 2.0f * i);

    case REACH: {    // 依次倒向一侧并停住，带一点轻微的摆动
      float s = smooth01((float)t / (float)g.periodMs - 0.6f * x);
      return g.dir * (0.9f * s + 0.1f * s * sinf(TWO_PI_F * phase * 0.5f));
    }

    case HEARTBEAT: {  // 两下快速跳动，然后停顿
      float p = phase - floorf(phase);
      // 脉冲宽度要足够宽，否则受限速影响，舵机来不及转到位
      float b1 = expf(-powf((p - 0.12f) / 0.07f, 2));
      float b2 = expf(-powf((p - 0.40f) / 0.07f, 2)) * 0.7f;
      return b1 + b2;
    }

    case SCAN: {     // 舵机一个接一个地动，像在寻找
      float p = phase * NUM;
      int active = ((int)floorf(p)) % NUM;
      int idx = (g.dir < 0) ? (NUM - 1 - i) : i;
      if (idx != active) return 0;
      return sinf(3.1415927f * (p - floorf(p)));
    }

    default:
      return 0;
  }
}

// ================= 指令处理 =================
void startGesture(Mode m, int amp, long periodMs, long durMs, int dir) {
  g.mode     = m;
  g.amp      = clampf(amp, 0, 100) / 100.0f;
  g.periodMs = (uint32_t)constrain(periodMs, 150L, 20000L);
  g.durMs    = (uint32_t)constrain(durMs, 0L, 60000L);
  g.dir      = (dir < 0) ? -1 : 1;
  g.startMs  = millis();
  g.active   = (m != STILL);
  manual      = false;
  pendingDone = true;
}

int findMode(const char *name) {
  for (int m = 0; m < MODE_COUNT; m++)
    if (strcasecmp(name, MODE_NAMES[m]) == 0) return m;
  return -1;
}

void handleLine(char *s) {
  char *tok = strtok(s, " \t");
  if (!tok) return;
  char cmd = toupper(tok[0]);

  if (cmd == 'M') {
    char *name = strtok(nullptr, " \t");
    int m = name ? findMode(name) : -1;
    if (m < 0) { Serial.println("ERR unknown mode (send L to list)"); return; }
    long v[4] = {50, 2000, 5000, 1};                 // amp, period, dur, dir 的默认值
    for (int k = 0; k < 4; k++) {
      char *t = strtok(nullptr, " \t");
      if (!t) break;
      v[k] = atol(t);
    }
    startGesture((Mode)m, (int)v[0], v[1], v[2], (int)v[3]);
    Serial.println("OK");

  } else if (cmd == 'P') {
    int count = 0;
    char *t;
    while ((t = strtok(nullptr, " \t")) != nullptr) {
      if (count < NUM) manualTarget[count] = constrain(atoi(t), ANGLE_MIN, ANGLE_MAX);
      count++;
    }
    if (count != NUM) { Serial.printf("ERR need %d angles, got %d\n", NUM, count); return; }
    g.active = false;
    manual = true;
    pendingDone = true;
    Serial.println("OK");

  } else if (cmd == 'H') {
    startGesture(STILL, 0, 2000, 0, 1);
    Serial.println("OK");

  } else if (cmd == '?') {
    Serial.printf("STATE %s", g.active ? MODE_NAMES[g.mode] : (manual ? "manual" : "idle"));
    for (int i = 0; i < NUM; i++) Serial.printf(" %d", (int)lroundf(pos[i]));
    Serial.println();

  } else if (cmd == 'L') {
    Serial.print("MODES");
    for (int m = 0; m < MODE_COUNT; m++) Serial.printf(" %s", MODE_NAMES[m]);
    Serial.println();

  } else {
    Serial.println("ERR unknown command");
  }
}

char lineBuf[96];
int  lineLen = 0;

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      handleLine(lineBuf);
      lineLen = 0;
    } else if (lineLen < (int)sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;
      Serial.println("ERR line too long");
    }
  }
}

// ================= 每 20ms 执行一次 =================
void controlTick() {
  uint32_t now = millis();
  float target[NUM];

  if (g.active) {
    uint32_t t = now - g.startMs;
    if (g.durMs > 0 && t >= g.durMs) {
      g.active = false;                                // 播放结束，回到中间
    } else {
      float env = clampf((float)t / FADE_MS, 0, 1);    // 淡入
      if (g.durMs > 0) env = fminf(env, clampf((float)(g.durMs - t) / FADE_MS, 0, 1));  // 淡出
      for (int i = 0; i < NUM; i++)
        target[i] = CENTER + centerTrim[i] + env * g.amp * MAX_SWING * patternOffset(i, t);
    }
  }
  if (!g.active) {
    for (int i = 0; i < NUM; i++)
      target[i] = manual ? manualTarget[i] : (float)(CENTER + centerTrim[i]);
  }

  bool arrived = true;
  for (int i = 0; i < NUM; i++) {
    float tgt  = clampf(target[i], ANGLE_MIN, ANGLE_MAX);
    float diff = clampf(tgt - pos[i], -MAX_STEP, MAX_STEP);   // 限速
    pos[i] += diff;
    if (fabsf(tgt - pos[i]) > 0.5f) arrived = false;
    servos[i].write((int)lroundf(pos[i]));
  }

  if (pendingDone && !g.active && arrived) {
    pendingDone = false;
    Serial.println("DONE");
  }
}

void setup() {
  Serial.begin(115200);
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  for (int i = 0; i < NUM; i++) {
    servos[i].setPeriodHertz(50);
    servos[i].attach(SERVO_PINS[i], 500, 2400);   // SG92R 常用脉宽范围
    pos[i] = CENTER + centerTrim[i];
    servos[i].write((int)pos[i]);
  }
  Serial.println("READY");
}

void loop() {
  readSerial();
  uint32_t now = millis();
  if (now - lastTick >= TICK_MS) {
    lastTick = now;
    controlTick();
  }
}
