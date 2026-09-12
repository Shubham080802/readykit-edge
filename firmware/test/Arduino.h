/* Minimal Arduino.h stub so protocol.h can be compiled and exercised on a
 * host machine. Only what protocol.h actually touches. Not for flashing. */
#ifndef ARDUINO_H_STUB
#define ARDUINO_H_STUB
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

struct SerialStub {
  void write(const char *s) { fputs(s, stdout); }
};
static SerialStub Serial;
#endif
