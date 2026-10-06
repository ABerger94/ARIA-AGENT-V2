/*
 * ARIA robot body firmware — Arduino Nano (ATmega328P / CH340 clone)
 *
 * Gives ARIA a physical desktop body with ZERO soldering:
 *   1. Plug the Nano into a Nano sensor shield (screw/plug headers, no solder).
 *   2. Plug head servos into the shield's D9 (pan) / D10 (tilt) headers.
 *   3. (Phase 2) Plug continuous-rotation wheel servos into D5 / D6.
 *   4. Power the servos from a 4xAA battery pack into the shield's servo
 *      power terminal block (NOT from USB — servos brown-out USB power).
 *   5. Optional face: 1.3" SH1106 OLED (128x64, I2C) on SDA=A4 / SCL=A5.
 *      Set FACE_OLED to 0 to build without the display.
 *   6. Flash this sketch once via Arduino IDE (Tools > Board > Arduino Nano,
 *      Processor > ATmega328P (Old Bootloader) for most clones).
 *      With FACE_OLED=1, install the "U8g2" library first
 *      (Sketch > Include Library > Manage Libraries > search "U8g2").
 *
 * Serial protocol @ 115200 baud, one command per line:
 *   P<pan>T<tilt>\n    head servos — pan 0-180, tilt 0-90.  e.g. "P90T45"
 *   W<l>,<r>\n        wheels — each -100..100 (continuous-rotation servos,
 *                      0 = stopped).                        e.g. "W-60,60"
 *   S\n               stop wheels + center head
 *
 * Matches aria/tools/hardware_proto.py (encode_servo / encode_drive /
 * encode_stop) and the move_head_servos / drive_wheels / body_stop tools.
 */

// ---- Face: animated OLED eyes + smile (optional) ----
#define FACE_OLED 1   // set to 0 to build without the display

#include <Servo.h>

#if FACE_OLED
#include <U8g2lib.h>
// 1.3" 128x64 SH1106 OLED over I2C (e.g. Inland KS0056 set to I2C mode).
// Nano I2C pins: SDA = A4, SCL = A5. Page-buffer mode keeps RAM use tiny.
U8G2_SH1106_128X64_NONAME_1_HW_I2C u8g2(U8G2_R0, /* reset=*/ U8X8_PIN_NONE);

static const uint8_t EYE_W = 26, EYE_H = 34, EYE_R = 7, EYE_Y = 6;
static const uint8_t EYE_LX = 24, EYE_RX = 78;

int pupTX = 0, pupTY = 0;   // pupil target offset (px)
int pupX = 0, pupY = 0;     // pupil current offset, eased toward target
unsigned long faceLast = 0;
const unsigned long FACE_MS = 60;      // face redraw cadence
unsigned long blinkAt = 3000;          // next scheduled blink (ms)
unsigned long blinkEnd = 0;            // current blink release time (ms)

// Declared below with the motion state; faceTick reads the head targets.
extern int panTgt, tiltTgt;

void faceTick(unsigned long now) {
  if (now - faceLast < FACE_MS) return;
  faceLast = now;

  // Pupils follow where the head is told to look — eyes glance with the neck.
  pupTX = map(panTgt, 0, 180, -8, 8);
  pupTY = map(tiltTgt, 0, 90, 5, -5);
  pupX += (pupTX - pupX) / 2;
  pupY += (pupTY - pupY) / 2;

  if (now >= blinkAt) {                // schedule the next blink
    blinkEnd = now + 140;
    blinkAt = now + 2500UL + random(2500UL);
  }
  bool closed = now < blinkEnd;

  u8g2.firstPage();
  do {
    if (closed) {
      // blink: eyes become thin lines
      u8g2.drawBox(EYE_LX, EYE_Y + EYE_H / 2 - 2, EYE_W, 4);
      u8g2.drawBox(EYE_RX, EYE_Y + EYE_H / 2 - 2, EYE_W, 4);
    } else {
      u8g2.drawRBox(EYE_LX, EYE_Y, EYE_W, EYE_H, EYE_R);
      u8g2.drawRBox(EYE_RX, EYE_Y, EYE_W, EYE_H, EYE_R);
      u8g2.setDrawColor(0);            // pupils punch out of the white eyes
      u8g2.drawBox(EYE_LX + EYE_W / 2 - 5 + pupX, EYE_Y + EYE_H / 2 - 7 + pupY, 10, 14);
      u8g2.drawBox(EYE_RX + EYE_W / 2 - 5 + pupX, EYE_Y + EYE_H / 2 - 7 + pupY, 10, 14);
      u8g2.setDrawColor(1);
    }
    // smile
    u8g2.drawCircle(64, 46, 12, U8G2_DRAW_LOWER_LEFT | U8G2_DRAW_LOWER_RIGHT);
  } while (u8g2.nextPage());
}
#endif

// ---- Pin map (PWM-capable pins on the Nano) ----
static const uint8_t PIN_PAN    = 9;    // head pan servo (SG90)
static const uint8_t PIN_TILT   = 10;   // head tilt servo (SG90)
static const uint8_t PIN_WHEEL_L = 5;   // left wheel (FS90R continuous)
static const uint8_t PIN_WHEEL_R = 6;   // right wheel (FS90R continuous)
static const uint8_t PIN_LED    = 13;   // heartbeat

Servo panServo, tiltServo, wheelL, wheelR;

// Eased head motion: current -> target a few degrees per tick (non-blocking).
int panCur = 90, tiltCur = 45;
int panTgt = 90, tiltTgt = 45;
unsigned long lastStep = 0;
const unsigned long STEP_MS = 15;   // ~4 deg per 15ms => smooth, not sluggish

char lineBuf[32];
uint8_t lineLen = 0;

int clampInt(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// Continuous-rotation servo: -100..100 -> 0..180us-style angle, 90 = stop.
int wheelToAngle(int v) {
  v = clampInt(v, -100, 100);
  return 90 + (v * 90) / 100;
}

void handleLine(char *line) {
  if (line[0] == 'P') {
    // P<pan>T<tilt>
    char *t = strchr(line, 'T');
    if (t) {
      *t = '\0';
      panTgt  = clampInt(atoi(line + 1), 0, 180);
      tiltTgt = clampInt(atoi(t + 1), 0, 90);
      Serial.print(F("ok head "));
      Serial.print(panTgt);
      Serial.print(' ');
      Serial.println(tiltTgt);
    }
  } else if (line[0] == 'W') {
    // W<l>,<r>
    char *c = strchr(line, ',');
    if (c) {
      *c = '\0';
      int l = clampInt(atoi(line + 1), -100, 100);
      int r = clampInt(atoi(c + 1), -100, 100);
      wheelL.write(wheelToAngle(l));
      wheelR.write(wheelToAngle(r));
      Serial.print(F("ok wheels "));
      Serial.print(l);
      Serial.print(' ');
      Serial.println(r);
    }
  } else if (line[0] == 'S') {
    wheelL.write(90);
    wheelR.write(90);
    panTgt = 90;
    tiltTgt = 45;
    Serial.println(F("ok stop"));
  }
}

void setup() {
  Serial.begin(115200);
#if FACE_OLED
  u8g2.begin();
  randomSeed(analogRead(A0));
#endif
  panServo.attach(PIN_PAN);
  tiltServo.attach(PIN_TILT);
  wheelL.attach(PIN_WHEEL_L);
  wheelR.attach(PIN_WHEEL_R);
  wheelL.write(90);   // wheels stopped
  wheelR.write(90);
  panServo.write(panCur);
  tiltServo.write(tiltCur);
  pinMode(PIN_LED, OUTPUT);
  Serial.println(F("aria-body ready"));
}

void loop() {
  // --- serial command intake ---
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = ch;
    }
  }

  // --- eased head motion ---
  unsigned long now = millis();
  if (now - lastStep >= STEP_MS) {
    lastStep = now;
    bool moved = false;
    if (panCur < panTgt)      { panCur = min(panCur + 4, panTgt); moved = true; }
    else if (panCur > panTgt) { panCur = max(panCur - 4, panTgt); moved = true; }
    if (tiltCur < tiltTgt)      { tiltCur = min(tiltCur + 4, tiltTgt); moved = true; }
    else if (tiltCur > tiltTgt) { tiltCur = max(tiltCur - 4, tiltTgt); moved = true; }
    if (moved) {
      panServo.write(panCur);
      tiltServo.write(tiltCur);
    }
  }

#if FACE_OLED
  // --- animated face: eyes track the head, blink on their own ---
  faceTick(now);
#endif

  // --- heartbeat LED (1 Hz) so you can see it's alive ---
  digitalWrite(PIN_LED, (millis() / 500) % 2 == 0 ? HIGH : LOW);
}
