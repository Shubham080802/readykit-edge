/*
 * ReadyKit Edge - indicator rendering
 *
 * Given a verdict indication, the latch state and the current millisecond,
 * decide what the two onboard RGB LEDs should show.
 *
 * This is deliberately pure: no Arduino calls, no globals, no I/O, and time
 * arrives as an argument. So the panel can be compiled and asserted on a host
 * machine (firmware/test/test_indicators.cpp) rather than verified by
 * squinting at a board - which matters when there is one board, no spares,
 * and a queue of judges.
 *
 * Channels here mean "lit". The sketch inverts on the way out, because the
 * UNO Q's onboard RGB LEDs are active-LOW.
 */
#ifndef READYKIT_INDICATORS_H
#define READYKIT_INDICATORS_H

#include <stdint.h>

enum LatchState : uint8_t { LATCH_ENGAGED = 0, LATCH_RELEASED };

enum Indicator : uint8_t {
  IND_OFF = 0,  /* Nothing to say. */
  IND_PASS,     /* Kit is complete. */
  IND_FAIL,     /* Kit is demonstrably non-compliant. */
  IND_HOLD,     /* Compliance could not be established. Not the same thing. */
  IND_STALE     /* The host is gone; nothing at all is known. */
};

/* Blink rates. Each state differs from the others in BOTH colour and rate,
 * so the panel still reads correctly to someone colour-blind, and still reads
 * correctly in a photograph where rate is invisible. */
static const uint32_t RK_FAIL_BLINK_MS   = 250;
static const uint32_t RK_HOLD_PULSE_MS   = 900;
static const uint32_t RK_STALE_WIGWAG_MS = 600;

struct RkRgb {
  bool r;
  bool g;
  bool b;
};

struct RkPanel {
  RkRgb latch;    /* Is the enclosure locked right now? */
  RkRgb verdict;  /* What did the model conclude? */
  bool  buzzer;
};

static inline RkRgb rkRgb(bool r, bool g, bool b) {
  RkRgb colour;
  colour.r = r;
  colour.g = g;
  colour.b = b;
  return colour;
}

static inline RkRgb rkDark(void)  { return rkRgb(false, false, false); }
static inline RkRgb rkRed(void)   { return rkRgb(true,  false, false); }
static inline RkRgb rkGreen(void) { return rkRgb(false, true,  false); }
static inline RkRgb rkAmber(void) { return rkRgb(true,  true,  false); }
static inline RkRgb rkBlue(void)  { return rkRgb(false, false, true);  }

/*
 * The two LEDs answer two different questions, and keeping them apart is the
 * whole point of having two.
 *
 * The latch LED answers "is it locked?" and answers it from the latch state
 * alone - never from the verdict. That light must never disagree with the
 * physical lock, because it is the one a person acts on.
 *
 * The verdict LED answers "what did the model conclude?", including the case
 * where it concluded nothing. Those are separate questions, and a kit that
 * cannot be read (amber) sitting beside a latch that stayed red is the
 * fail-closed rule made visible in one glance.
 */
static inline RkPanel rkRenderPanel(Indicator indicator,
                                    LatchState latch,
                                    bool buzzerArmed,
                                    uint32_t nowMs) {
  RkPanel panel;

  panel.latch = (latch == LATCH_RELEASED) ? rkGreen() : rkRed();
  panel.buzzer = false;

  switch (indicator) {
    case IND_PASS:
      panel.verdict = rkGreen();
      break;

    case IND_FAIL: {
      /* Fast, red, buzzer in step with the blink so the two agree. */
      const bool on = ((nowMs / RK_FAIL_BLINK_MS) % 2) == 0;
      panel.verdict = on ? rkRed() : rkDark();
      panel.buzzer = buzzerArmed && on;
      break;
    }

    case IND_HOLD: {
      /* Amber, and slower. "I cannot see your kit" must not look like "your
       * kit is wrong" - the operator's next move differs. On the old
       * discrete-LED build both were red and differed only in rate, which is
       * the cue people read last. Colour is the cue they read first. */
      const bool on = ((nowMs / RK_HOLD_PULSE_MS) % 2) == 0;
      panel.verdict = on ? rkAmber() : rkDark();
      break;
    }

    case IND_STALE: {
      /* Blue, alternating. The host is gone, so nothing at all is known about
       * the kit in front of the camera - different again from a kit that was
       * read and failed. */
      const bool phase = ((nowMs / RK_STALE_WIGWAG_MS) % 2) == 0;
      panel.verdict = phase ? rkBlue() : rkDark();
      break;
    }

    case IND_OFF:
    default:
      panel.verdict = rkDark();
      break;
  }

  return panel;
}

#endif /* READYKIT_INDICATORS_H */
