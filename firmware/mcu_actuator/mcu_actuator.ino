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
 * Wiring: none. Everything the demonstration needs is already on the board.
 * The header pins below are the deployed unit's lines - relay coil, sounder,
 * discrete lamps - and they are driven whether or not anything is attached,
 * so the same binary runs on a bare board and in an enclosure.
 *
 *   D2  Green lamp      (optional)   pass
 *   D3  Red lamp        (optional)   fail / hold / stale
 *   D4  Piezo sounder   (optional)   fail only
 *   D5  Relay module IN (optional)   12V solenoid latch
 */

#include "protocol.h"
#include "indicators.h"

/* -------------------------------------------------------------- onboard */

/*
 * The UNO Q carries two RGB LEDs wired straight to the STM32U585 (PH10-PH12
 * and PH13-PH15), which is exactly the two signals this system has to show:
 * what the lock is doing, and what the model concluded. They are active-LOW.
 *
 * Arduino cores disagree about what these channels are called, and guessing
 * wrong means a sketch that will not compile - at a bench, with one board and
 * no spares, an hour before a demo. So every name is guarded and there is a
 * fallback at each step. Colour is a nicety; compiling is not.
 *
 * To pin the mapping by hand, define RK_VERDICT_R and friends with -D or just
 * above this block, and the guards step aside.
 */
#if !defined(RK_VERDICT_R)
  #if defined(LEDR) && defined(LEDG) && defined(LEDB)
    #define RK_VERDICT_R LEDR
    #define RK_VERDICT_G LEDG
    #define RK_VERDICT_B LEDB
  #elif defined(LED_RED) && defined(LED_GREEN) && defined(LED_BLUE)
    #define RK_VERDICT_R LED_RED
    #define RK_VERDICT_G LED_GREEN
    #define RK_VERDICT_B LED_BLUE
  #endif
#endif

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

/* Applies whatever rkRenderPanel() decided. Driven from millis() on every
 * pass of loop() rather than from a blocking blink routine, so the alarm
 * pattern never costs us serial responsiveness.
 *
 * The decision of what to show lives in indicators.h and is tested on a host
 * machine; this function only knows how to push it at pins. */

#if defined(RK_VERDICT_R)
static void writeRgb(uint8_t rPin, uint8_t gPin, uint8_t bPin, RkRgb colour) {
  /* Active LOW: a channel lights when its pin is pulled down. */
  digitalWrite(rPin, colour.r ? LOW : HIGH);
  digitalWrite(gPin, colour.g ? LOW : HIGH);
  digitalWrite(bPin, colour.b ? LOW : HIGH);
}
#endif

static void updateIndicators() {
  const RkPanel panel = rkRenderPanel(indicator, latch, buzzerOn, millis());

#if defined(RK_VERDICT_R)
  writeRgb(RK_VERDICT_R, RK_VERDICT_G, RK_VERDICT_B, panel.verdict);
#endif

#if defined(LED_BUILTIN)
  /* The latch, on whatever single lamp is left. Lit means OPEN, deliberately:
   * a dead, unpowered or unflashed board then reads as locked - which is the
   * truth, because the relay line falls to its de-energised state at exactly
   * the same moment. The failure of this indicator and the failure of the
   * lock agree. */
  digitalWrite(LED_BUILTIN, latch == LATCH_RELEASED ? HIGH : LOW);
#endif

  /* The deployed unit's discrete lines. Nothing is attached on a bare board
   * and driving them costs nothing, so one binary covers both. */
  digitalWrite(PIN_GREEN_LED, panel.verdict.g ? HIGH : LOW);
  digitalWrite(PIN_RED_LED, panel.verdict.r ? HIGH : LOW);
  digitalWrite(PIN_BUZZER, panel.buzzer ? HIGH : LOW);
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

#if defined(RK_VERDICT_R)
  pinMode(RK_VERDICT_R, OUTPUT);
  pinMode(RK_VERDICT_G, OUTPUT);
  pinMode(RK_VERDICT_B, OUTPUT);
  writeRgb(RK_VERDICT_R, RK_VERDICT_G, RK_VERDICT_B, rkDark());
#endif

#if defined(LED_BUILTIN)
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);   /* latch is engaged; lamp says so */
#endif

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
