/*
 * ReadyKit Edge - Host Link framing for the STM32U585 MCU core.
 *
 * This is the C side of src/readykit/protocol.py. The two must agree exactly:
 * same polynomial, same field order, same checksum coverage. If you change one,
 * change the other, and re-run tests/test_protocol.py.
 *
 *   Inbound:   RK1 <seq> <COMMAND> <payload> <CRC8>\n
 *   Outbound:  ACK <seq> <STATUS>\n
 *
 * The CRC covers every byte before the space that precedes the checksum field.
 *
 * No dynamic allocation, no String, no delay(). This runs in a loop that must
 * stay responsive while a solenoid is energised.
 */

#ifndef READYKIT_PROTOCOL_H
#define READYKIT_PROTOCOL_H

#include <Arduino.h>

#define RK_PROTOCOL_VERSION "RK1"
#define RK_MAX_LINE 128
#define RK_MAX_PAYLOAD 64

enum RkCommand : uint8_t {
  RK_CMD_NONE = 0,
  RK_CMD_RELEASE,
  RK_CMD_REJECT,
  RK_CMD_HOLD,
  RK_CMD_PING,
  RK_CMD_RESET
};

enum RkAckStatus : uint8_t {
  RK_ACK_OK = 0,
  RK_ACK_BAD_CRC,
  RK_ACK_BAD_FRAME,
  RK_ACK_REFUSED
};

struct RkFrame {
  uint16_t seq;
  RkCommand command;
  char payload[RK_MAX_PAYLOAD + 1];
};

/* CRC-8/ATM: polynomial 0x07, init 0x00, no reflection, no final xor.
 * Computed bitwise rather than from a table - 256 bytes of flash is worth
 * more here than the handful of cycles this costs on a 160MHz M33. */
inline uint8_t rkCrc8(const char *data, size_t length) {
  uint8_t crc = 0x00;
  for (size_t i = 0; i < length; i++) {
    crc ^= (uint8_t)data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

inline const char *rkAckName(RkAckStatus status) {
  switch (status) {
    case RK_ACK_OK:        return "OK";
    case RK_ACK_BAD_CRC:   return "BAD_CRC";
    case RK_ACK_REFUSED:   return "REFUSED";
    case RK_ACK_BAD_FRAME:
    default:               return "BAD_FRAME";
  }
}

inline void rkSendAck(uint16_t seq, RkAckStatus status) {
  char out[32];
  snprintf(out, sizeof(out), "ACK %u %s\n", (unsigned)seq, rkAckName(status));
  Serial.write(out);
}

inline RkCommand rkParseCommand(const char *token) {
  if (strcmp(token, "RELEASE") == 0) return RK_CMD_RELEASE;
  if (strcmp(token, "REJECT")  == 0) return RK_CMD_REJECT;
  if (strcmp(token, "HOLD")    == 0) return RK_CMD_HOLD;
  if (strcmp(token, "PING")    == 0) return RK_CMD_PING;
  if (strcmp(token, "RESET")   == 0) return RK_CMD_RESET;
  return RK_CMD_NONE;
}

/*
 * Parse one complete line into `out`.
 *
 * Returns RK_ACK_OK when the frame verified. Anything else is the status to
 * send back, and `out` must not be acted on. The parser mutates `line` in
 * place while tokenising, which is fine - the caller owns a scratch buffer.
 */
inline RkAckStatus rkDecodeCommand(char *line, size_t length, RkFrame *out) {
  while (length > 0 && (line[length - 1] == '\r' || line[length - 1] == '\n')) {
    line[--length] = '\0';
  }
  if (length == 0) return RK_ACK_BAD_FRAME;

  /* Split off the trailing checksum field. */
  char *checksumField = strrchr(line, ' ');
  if (checksumField == NULL) return RK_ACK_BAD_FRAME;
  *checksumField = '\0';
  checksumField++;

  char *end = NULL;
  long received = strtol(checksumField, &end, 16);
  if (end == checksumField || *end != '\0') return RK_ACK_BAD_FRAME;

  if ((uint8_t)received != rkCrc8(line, strlen(line))) return RK_ACK_BAD_CRC;

  /* version */
  char *cursor = line;
  char *version = cursor;
  char *space = strchr(cursor, ' ');
  if (space == NULL) return RK_ACK_BAD_FRAME;
  *space = '\0';
  cursor = space + 1;
  if (strcmp(version, RK_PROTOCOL_VERSION) != 0) return RK_ACK_BAD_FRAME;

  /* sequence */
  char *seqToken = cursor;
  space = strchr(cursor, ' ');
  if (space == NULL) return RK_ACK_BAD_FRAME;
  *space = '\0';
  cursor = space + 1;
  long seq = strtol(seqToken, &end, 10);
  if (end == seqToken || *end != '\0' || seq < 0 || seq > 65535) {
    return RK_ACK_BAD_FRAME;
  }
  out->seq = (uint16_t)seq;

  /* command, and whatever remains is the payload */
  char *commandToken = cursor;
  space = strchr(cursor, ' ');
  if (space != NULL) {
    *space = '\0';
    cursor = space + 1;
  } else {
    cursor = commandToken + strlen(commandToken);
  }

  out->command = rkParseCommand(commandToken);
  if (out->command == RK_CMD_NONE) return RK_ACK_BAD_FRAME;

  strncpy(out->payload, cursor, RK_MAX_PAYLOAD);
  out->payload[RK_MAX_PAYLOAD] = '\0';
  return RK_ACK_OK;
}

#endif  /* READYKIT_PROTOCOL_H */
