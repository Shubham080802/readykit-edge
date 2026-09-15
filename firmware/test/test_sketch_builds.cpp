/*
 * Compile the actual sketch.
 *
 * Nothing else in this repository does. A sketch that does not build is
 * discovered at a bench, with one board, minutes before it is needed - so the
 * cheapest possible check is worth having: pull the real .ino into a host
 * translation unit and make the compiler read every line of it, with
 * -Wall -Wextra -Werror.
 *
 * This proves it compiles and that setup()/loop() are wired to real symbols.
 * It does not prove anything about the STM32U585 toolchain or the board's own
 * pin macros - only flashing does that. It catches the typo, not the target.
 */
#include "Arduino.h"
#include "../mcu_actuator/mcu_actuator.ino"

int main() {
  setup();
  for (int i = 0; i < 4; ++i) {
    rkStubClock() += 250;
    loop();
  }
  printf("sketch built and ran %d passes\n", 4);
  return 0;
}
