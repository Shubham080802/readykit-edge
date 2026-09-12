/*
 * Cross-check the MCU parser against frames produced by the Python encoder.
 *
 * The two implementations of this protocol live in different languages on
 * different processors, and the whole safety argument assumes they agree.
 * This harness reads frames and expected outcomes on stdin, runs the real
 * rkDecodeCommand() over them, and prints what the firmware would conclude.
 * tests/test_firmware_protocol.py generates the input and checks the output.
 */
#include "Arduino.h"
#include "../mcu_actuator/protocol.h"

int main() {
  char line[RK_MAX_LINE];

  while (fgets(line, sizeof(line), stdin) != NULL) {
    size_t len = strlen(line);
    if (len == 0) continue;

    /* CRC self-check mode: "CRC <text>" prints the checksum of <text>. */
    if (strncmp(line, "CRC ", 4) == 0) {
      char *body = line + 4;
      size_t blen = strlen(body);
      while (blen > 0 && (body[blen-1] == '\n' || body[blen-1] == '\r')) {
        body[--blen] = '\0';
      }
      printf("CRC %02X\n", rkCrc8(body, blen));
      continue;
    }

    RkFrame frame;
    memset(&frame, 0, sizeof(frame));
    RkAckStatus status = rkDecodeCommand(line, len, &frame);

    if (status != RK_ACK_OK) {
      printf("REJECT %s\n", rkAckName(status));
    } else {
      printf("ACCEPT %u %d %s\n",
             (unsigned)frame.seq, (int)frame.command, frame.payload);
    }
  }
  return 0;
}
