/* Minimal Arduino.h stub so the firmware can be compiled and exercised on a
 * host machine. Only what the sketch and protocol.h actually touch, and no
 * more - this is a compile-and-assert harness, never something to flash.
 *
 * millis() is backed by a settable clock so time-dependent behaviour can be
 * driven from a test instead of waited out. */
#ifndef ARDUINO_H_STUB
#define ARDUINO_H_STUB
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLUP 2

/* Function-local static rather than a file-scope one, so an including
 * translation unit that never touches the clock draws no unused warnings
 * under -Wall -Wextra -Werror. */
static inline uint32_t &rkStubClock() {
  static uint32_t now = 0;
  return now;
}

static inline uint32_t millis() { return rkStubClock(); }
static inline void pinMode(uint8_t, uint8_t) {}
static inline void digitalWrite(uint8_t, uint8_t) {}
static inline int digitalRead(uint8_t) { return LOW; }

struct SerialStub {
  void write(const char *s) { fputs(s, stdout); }
  void begin(unsigned long) {}
  int available() { return 0; }
  int read() { return -1; }
};
static SerialStub Serial;
#endif
