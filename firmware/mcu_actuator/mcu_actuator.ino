/*
 * ReadyKit Edge - Actuator Node
 * Target: Arduino UNO Q, STM32U585 microcontroller core
 *
 * This sketch holds a physical lock closed. It is written on the assumption
 * that the Inspection Host may crash, the USB cable may be pulled, and the
 * line may be noisy - and that none of those may leave an enclosure open.
 *
 * Three rules govern everything here:
 *
 *   1. ENGAGED is the resting state. Power-on, reset, a stale link, and an
 *      expired hold all land there. RELEASED is only ever granted, never
 *      defaulted to.
 *   2. Nothing is acted on unverified. Every frame is checksummed, and a
 *      sequence number already acted on is refused so a retransmitted
 *      RELEASE cannot actuate twice.
 *   3. loop() never blocks. No delay(), anywhere. A blocking hold means the
 *      MCU is deaf to serial for the duration - including deaf to the RESET
 *      that would have closed the latch.
 *
 * This mirrors VirtualActuatorNode in src/readykit/bridge/loopback.py, which
 * is where the behaviour is tested. Change one, change the other.
 *
 * Wiring (per the hardware pinout):
 *   D2  Green LED  + 220R to GND     pass
 *   D3  Red LED    + 220R to GND     fail / hold / stale
 *   D4  Piezo buzzer (active)        fail only
 *   D5  Relay module IN              12V solenoid latch
 *   GND Common ground rail
 */

#include "protocol.h"

static const uint8_t PIN_GREEN_LED = 2;
static const uint8_t PIN_RED_LED   = 3;
static const uint8_t PIN_BUZZER    = 4;
static const uint8_t PIN_SOLENOID  = 5;

/* The relay module is active-HIGH and the solenoid is a fail-secure latch:
 * de-energised means locked. LOW is therefore the safe state, and it is what
 * the pin falls to on reset or power loss. */
static const uint8_t SOLENOID_LOCKED   = LOW;
static const uint8_t SOLENOID_UNLOCKED = HIGH;

/* Must be shorter than the host's inspection interval is long, and the host
 * must heartbeat faster than this. Keep in step with
 * VirtualActuatorNode.link_timeout_ms. */
static const uint32_t LINK_TIMEOUT_MS = 2000;
static const uint32_t MAX_HOLD_MS     = 60000;

enum LatchState : uint8_t { LATCH_ENGAGED = 0, LATCH_RELEASED };
enum Indicator : uint8_t {
  IND_OFF = 0, IND_PASS, IND_FAIL, IND_HOLD, IND_STALE
};

static LatchState latch          = LATCH_ENGAGED;
static Indicator  indicator      = IND_OFF;
static bool       buzzerOn       = false;

/* The last indication a verdict actually asked for, kept apart from the
 * displayed one so that going stale can overlay the display without
 * destroying it. A FAIL that vanishes because the cable was briefly unplugged
 * is a failure the operator never sees. */
static Indicator  verdictIndicator = IND_OFF;

static uint32_t   releaseExpires = 0;
static bool       holdActive     = false;
static uint32_t   lastContactMs  = 0;
static bool       haveLastSeq    = false;
static uint16_t   lastSeq        = 0;

static char   lineBuffer[RK_MAX_LINE];
static size_t lineLength = 0;

/* ------------------------------------------------------------------ latch */

static void indicate(Indicator next, bool buzzer) {
  indicator = next;
  verdictIndicator = next;
  buzzerOn = buzzer;
}

static void engageLatch() {
  digitalWrite(PIN_SOLENOID, SOLENOID_LOCKED);
  latch = LATCH_ENGAGED;
  holdActive = false;
}

static void releaseLatch(uint32_t holdMs) {
  digitalWrite(PIN_SOLENOID, SOLENOID_UNLOCKED);
  latch = LATCH_RELEASED;
  releaseExpires = millis() + holdMs;
  holdActive = true;
}

/* ------------------------------------------------------------- indicators */

/* Driven from millis() on every pass of loop() rather than from a blocking
 * blink routine, so the alarm pattern never costs us serial responsiveness. */
static void updateIndicators() {
  const uint32_t now = millis();

  switch (indicator) {
    case IND_PASS:
      digitalWrite(PIN_GREEN_LED, HIGH);
      digitalWrite(PIN_RED_LED, LOW);
      break;

    case IND_FAIL: {
      /* Fast urgent blink, buzzer in step with it. */
      const bool on = ((now / 250) % 2) == 0;
      digitalWrite(PIN_GREEN_LED, LOW);
      digitalWrite(PIN_RED_LED, on ? HIGH : LOW);
      digitalWrite(PIN_BUZZER, (buzzerOn && on) ? HIGH : LOW);
      return;
    }

    case IND_HOLD: {
      /* Slow, silent pulse. "I cannot see your kit" must not look or sound
       * like "your kit is wrong" - the operator's next move differs. */
      const bool on = ((now / 900) % 2) == 0;
      digitalWrite(PIN_GREEN_LED, LOW);
      digitalWrite(PIN_RED_LED, on ? HIGH : LOW);
      break;
    }

    case IND_STALE: {
      /* Alternating amber-ish wig-wag: the host is gone, so nothing at all
       * is known about the kit in front of the camera. */
      const bool phase = ((now / 600) % 2) == 0;
      digitalWrite(PIN_GREEN_LED, phase ? HIGH : LOW);
      digitalWrite(PIN_RED_LED, phase ? LOW : HIGH);
      break;
    }

    case IND_OFF:
    default:
      digitalWrite(PIN_GREEN_LED, LOW);
      digitalWrite(PIN_RED_LED, LOW);
      break;
  }

  digitalWrite(PIN_BUZZER, LOW);
}

/* -------------------------------------------------------------- watchdog */

static void tick() {
  const uint32_t now = millis();

  /* Subtraction rather than comparison so the 49-day millis() rollover is
   * handled correctly by unsigned wraparound. */
  if (holdActive && (int32_t)(now - releaseExpires) >= 0) {
    engageLatch();
    if (verdictIndicator == IND_PASS) indicate(IND_OFF, false);
  }

  if ((uint32_t)(now - lastContactMs) > LINK_TIMEOUT_MS) {
    /* The host has gone quiet. Do not hold the last instruction - the last
     * instruction was about a kit nobody is watching any more. */
    if (latch == LATCH_RELEASED) {
      engageLatch();
    }
    /* Overlays the display only. verdictIndicator is untouched, so the
     * verdict is restored when the host comes back. */
    indicator = IND_STALE;
  }
}

/* --------------------------------------------------------------- commands */

static RkAckStatus applyRelease(const char *payload) {
  char *end = NULL;
  long holdMs = strtol(payload, &end, 10);
  if (end == payload || *end != '\0') return RK_ACK_BAD_FRAME;

  /* An out-of-range hold is a host-side bug whose consequence is an open
   * enclosure. Refuse rather than clamp - the host should hear about it. */
  if (holdMs <= 0 || (uint32_t)holdMs > MAX_HOLD_MS) return RK_ACK_REFUSED;

  releaseLatch((uint32_t)holdMs);
  indicate(IND_PASS, false);
  return RK_ACK_OK;
}

static RkAckStatus applyCommand(const RkFrame *frame) {
  switch (frame->command) {
    case RK_CMD_PING:
      /* Contact alone is the point. Recovering from stale restores the last
       * verdict's indication rather than blanking it - an indicator reading
       * "off" beside a sounding buzzer is a panel contradicting itself. */
      if (indicator == IND_STALE) indicator = verdictIndicator;
      return RK_ACK_OK;

    case RK_CMD_RESET:
      engageLatch();
      indicate(IND_OFF, false);
      return RK_ACK_OK;

    case RK_CMD_RELEASE:
      return applyRelease(frame->payload);

    case RK_CMD_REJECT:
      engageLatch();
      indicate(IND_FAIL, true);
      return RK_ACK_OK;

    case RK_CMD_HOLD:
      engageLatch();
      indicate(IND_HOLD, false);
      return RK_ACK_OK;

    default:
      return RK_ACK_BAD_FRAME;
  }
}

static void handleLine(char *line, size_t length) {
  RkFrame frame;
  RkAckStatus status = rkDecodeCommand(line, length, &frame);

  if (status != RK_ACK_OK) {
    /* Deliberately does NOT refresh lastContactMs: a stream of line noise
     * must not read as a live host and keep the watchdog satisfied. */
    rkSendAck(0, status);
    return;
  }

  lastContactMs = millis();

  if (haveLastSeq && frame.seq == lastSeq) {
    rkSendAck(frame.seq, RK_ACK_REFUSED);
    return;
  }
  haveLastSeq = true;
  lastSeq = frame.seq;

  rkSendAck(frame.seq, applyCommand(&frame));
}

static void readSerial() {
  while (Serial.available() > 0) {
    const int incoming = Serial.read();
    if (incoming < 0) return;

    const char ch = (char)incoming;
    if (ch == '\n') {
      lineBuffer[lineLength] = '\0';
      handleLine(lineBuffer, lineLength);
      lineLength = 0;
      continue;
    }

    if (lineLength < RK_MAX_LINE - 1) {
      lineBuffer[lineLength++] = ch;
    } else {
      /* Overlong line: discard it rather than truncating into something that
       * might parse. Resynchronise on the next newline. */
      lineLength = 0;
      rkSendAck(0, RK_ACK_BAD_FRAME);
    }
  }
}

/* ------------------------------------------------------------------- main */

void setup() {
  pinMode(PIN_SOLENOID, OUTPUT);
  digitalWrite(PIN_SOLENOID, SOLENOID_LOCKED);  /* before anything else */

  pinMode(PIN_GREEN_LED, OUTPUT);
  pinMode(PIN_RED_LED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_GREEN_LED, LOW);
  digitalWrite(PIN_RED_LED, LOW);
  digitalWrite(PIN_BUZZER, LOW);

  Serial.begin(115200);

  lastContactMs = millis();
  latch = LATCH_ENGAGED;
  indicator = IND_OFF;
  verdictIndicator = IND_OFF;
}

void loop() {
  readSerial();
  tick();
  updateIndicators();
}
