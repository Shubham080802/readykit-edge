/*
 * Exercise the real rkRenderPanel() on a host machine.
 *
 * The panel is what a person in front of the cabinet actually acts on, so
 * "red means locked" and "amber is not red" are safety claims, not cosmetics.
 * This harness reads panel queries on stdin and prints what the firmware
 * would light; tests/test_firmware_indicators.py supplies the cases and makes
 * the assertions.
 *
 * Input:  <indicator> <latch> <buzzerArmed> <nowMs>
 * Output: LATCH <rgb> VERDICT <rgb> BUZZER <0|1>     e.g. "R--", "RG-", "---"
 */
#include <stdio.h>
#include <string.h>
#include "../mcu_actuator/indicators.h"

static void formatRgb(RkRgb colour, char out[4]) {
  out[0] = colour.r ? 'R' : '-';
  out[1] = colour.g ? 'G' : '-';
  out[2] = colour.b ? 'B' : '-';
  out[3] = '\0';
}

int main() {
  char line[128];

  while (fgets(line, sizeof(line), stdin) != NULL) {
    unsigned indicator = 0;
    unsigned latch = 0;
    unsigned buzzerArmed = 0;
    unsigned long nowMs = 0;

    if (sscanf(line, "%u %u %u %lu",
               &indicator, &latch, &buzzerArmed, &nowMs) != 4) {
      continue;
    }

    const RkPanel panel = rkRenderPanel((Indicator)indicator,
                                        (LatchState)latch,
                                        buzzerArmed != 0,
                                        (uint32_t)nowMs);

    char latchRgb[4];
    char verdictRgb[4];
    formatRgb(panel.latch, latchRgb);
    formatRgb(panel.verdict, verdictRgb);

    printf("LATCH %s VERDICT %s BUZZER %d\n",
           latchRgb, verdictRgb, panel.buzzer ? 1 : 0);
    fflush(stdout);
  }
  return 0;
}
