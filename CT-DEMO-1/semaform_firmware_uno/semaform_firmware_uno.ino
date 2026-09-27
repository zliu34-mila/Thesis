#include <Servo.h>
#include <math.h>

// ================= 硬件配置 =================
// 舵机信号线接的数字引脚，按从左到右的顺序。不要用 0 和 1（那是串口）。
const int SERVO_PINS[] = {9, 6, 5, 3};
const int NUM = sizeof(SERVO_PINS) / sizeof(SERVO_PINS[0]);

// 每个舵机的中心微调（度）：装上摆臂后，如果 90° 时不正，就在这里修正
int centerTrim[8] = {0, 0, 0, 0, 0, 0, 0, 0};

const int   CENTER     = 90;
const int   ANGLE_MIN  = 10;       // 软件限位，避免顶到机械极限
const int   ANGLE_MAX  = 170;
const float MAX_SWING  = 70.0;     // amp=100 时，最大偏离中心 ±70°
const float MAX_STEP   = 8.0;      // 每个周期最多转 8°（= 400°/s），保护齿轮
const unsigned long TICK_MS = 20;  // 50 Hz
const unsigned long FADE_MS = 400; // 动作开始/结束时的淡入淡出

// ================= 动作库 =================
enum Mode { STILL, BREATHE, WAVE, RIPPLE, SHIVER, REACH, HEARTBEAT, SCAN, MODE_COUNT };
const char *MODE_NAMES[MODE_COUNT] = {
  "still", "breathe", "wave", "ripple", "shiver", "reach", "heartbeat", "scan"
};

Mode  gMode     = STILL;
float gAmp      = 0;        // 0~1
unsigned long gPeriodMs = 2000;
unsigned long gDurMs    = 0;  // 0 = 一直播放
int   gDir      = 1;
unsigned long gStartMs  = 0;
bool  gActive   = false;

Servo servos[8];
float pos[8];                 // 当前角度
float manualTarget[8];        // P 指令的目标
bool  manual      = false;
bool  pendingDone = false;    // 动作结束、回到中间后要回复 DONE
unsigned long lastTick = 0;

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
float smooth01(float s) { s = clampf(s, 0, 1); return s * s * (3 - 2 * s); }

// 舵机 i 在时刻 t（动作开始后的毫秒数）的偏移量，范围大约 -1 ~ 1
float patternOffset(int i, unsigned long t) {
  const float TWO_PI_F = 6.2831853;
  float phase = (float)t / (float)gPeriodMs;                  // 已经走过的周期数
  float x = (NUM > 1) ? (float)i / (float)(NUM - 1) : 0.0;    // 在这一排里的位置 0~1
  if (gDir < 0) x = 1.0 - x;                                  // 方向反过来

  switch (gMode) {
    case BREATHE:    // 全体同步、缓慢起伏
      return sinf(TWO_PI_F * phase);

    case WAVE:       // 从一侧传到另一侧
      return sinf(TWO_PI_F * (phase - 0.75 * x));

    case RIPPLE: {   // 从中间向两边扩散
      float d = fabsf(x - 0.5) * 2.0;                         // 0 = 中间，1 = 两端
      return sinf(TWO_PI_F * (phase - 0.5 * d));
    }

    case SHIVER:     // 快速小幅抖动：每个舵机用不同的频率叠加，看起来不规则
      return 0.6 * sinf(TWO_PI_F * phase * (1.0 + 0.37 * i) + i)
           + 0.4 * sinf(TWO_PI_F * phase * (2.3 + 0.21 * i) + 2.0 * i);

    case REACH: {    // 依次倒向一侧并停住，带一点轻微的摆动
      float s = smooth01((float)t / (float)gPeriodMs - 0.6 * x);
      return gDir * (0.9 * s + 0.1 * s * sinf(TWO_PI_F * phase * 0.5));
    }

    case HEARTBEAT: {  // 两下快速跳动，然后停顿
      float p = phase - floorf(phase);
      float b1 = expf(-powf((p - 0.12) / 0.07, 2));
      float b2 = expf(-powf((p - 0.40) / 0.07, 2)) * 0.7;
      return b1 + b2;
    }

    case SCAN: {     // 舵机一个接一个地动，像在寻找
      float p = phase * NUM;
      int active = ((int)floorf(p)) % NUM;
      int idx = (gDir < 0) ? (NUM - 1 - i) : i;
      if (idx != active) return 0;
      return sinf(3.1415927 * (p - floorf(p)));
    }

    default:
      return 0;
  }
}

// ================= 指令处理 =================
void startGesture(Mode m, long amp, long periodMs, long durMs, int dir) {
  gMode     = m;
  gAmp      = clampf(amp, 0, 100) / 100.0;
  gPeriodMs = constrain(periodMs, 150L, 20000L);
  gDurMs    = constrain(durMs, 0L, 60000L);
  gDir      = (dir < 0) ? -1 : 1;
  gStartMs  = millis();
  gActive   = (m != STILL);
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
    char *name = strtok(NULL, " \t");
    int m = name ? findMode(name) : -1;
    if (m < 0) { Serial.println(F("ERR unknown mode (send L to list)")); return; }
    long v[4] = {50, 2000, 5000, 1};                 // amp, period, dur, dir 的默认值
    for (int k = 0; k < 4; k++) {
      char *t = strtok(NULL, " \t");
      if (!t) break;
      v[k] = atol(t);
    }
    startGesture((Mode)m, v[0], v[1], v[2], (int)v[3]);
    Serial.println(F("OK"));

  } else if (cmd == 'P') {
    int count = 0;
    char *t;
    while ((t = strtok(NULL, " \t")) != NULL) {
      if (count < NUM) manualTarget[count] = constrain(atoi(t), ANGLE_MIN, ANGLE_MAX);
      count++;
    }
    if (count != NUM) {
      Serial.print(F("ERR need ")); Serial.print(NUM);
      Serial.print(F(" angles, got ")); Serial.println(count);
      return;
    }
    gActive = false;
    manual = true;
    pendingDone = true;
    Serial.println(F("OK"));

  } else if (cmd == 'H') {
    startGesture(STILL, 0, 2000, 0, 1);
    Serial.println(F("OK"));

  } else if (cmd == '?') {
    Serial.print(F("STATE "));
    Serial.print(gActive ? MODE_NAMES[gMode] : (manual ? "manual" : "idle"));
    for (int i = 0; i < NUM; i++) { Serial.print(' '); Serial.print(lroundf(pos[i])); }
    Serial.println();

  } else if (cmd == 'L') {
    Serial.print(F("MODES"));
    for (int m = 0; m < MODE_COUNT; m++) { Serial.print(' '); Serial.print(MODE_NAMES[m]); }
    Serial.println();

  } else {
    Serial.println(F("ERR unknown command"));
  }
}

char lineBuf[64];
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
      Serial.println(F("ERR line too long"));
    }
  }
}

// ================= 每 20ms 执行一次 =================
void controlTick() {
  unsigned long now = millis();
  float target[8];

  if (gActive) {
    unsigned long t = now - gStartMs;
    if (gDurMs > 0 && t >= gDurMs) {
      gActive = false;                                 // 播放结束，回到中间
    } else {
      float env = clampf((float)t / FADE_MS, 0, 1);    // 淡入
      if (gDurMs > 0) env = fminf(env, clampf((float)(gDurMs - t) / FADE_MS, 0, 1));  // 淡出
      for (int i = 0; i < NUM; i++)
        target[i] = CENTER + centerTrim[i] + env * gAmp * MAX_SWING * patternOffset(i, t);
    }
  }
  if (!gActive) {
    for (int i = 0; i < NUM; i++)
      target[i] = manual ? manualTarget[i] : (float)(CENTER + centerTrim[i]);
  }

  bool arrived = true;
  for (int i = 0; i < NUM; i++) {
    float tgt  = clampf(target[i], ANGLE_MIN, ANGLE_MAX);
    float diff = clampf(tgt - pos[i], -MAX_STEP, MAX_STEP);   // 限速
    pos[i] += diff;
    if (fabsf(tgt - pos[i]) > 0.5) arrived = false;
    servos[i].write((int)lroundf(pos[i]));
  }

  if (pendingDone && !gActive && arrived) {
    pendingDone = false;
    Serial.println(F("DONE"));
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM; i++) {
    pos[i] = CENTER + centerTrim[i];
    servos[i].attach(SERVO_PINS[i], 500, 2400);   // SG92R 常用脉宽范围
    servos[i].write((int)pos[i]);
  }
  Serial.println(F("READY"));
}

void loop() {
  readSerial();
  unsigned long now = millis();
  if (now - lastTick >= TICK_MS) {
    lastTick = now;
    controlTick();
  }
}
