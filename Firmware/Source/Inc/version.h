/*
 * version.h - firmware version, semver.
 *
 * Command 0xFD keeps the original encoding, (MAJOR << 4) | MINOR, so every
 * existing host still reads it; it cannot carry PATCH and caps MAJOR and MINOR
 * at 15. Command 0xFC (FIRMWARE_VERSION_EXT) adds the full version:
 * [schema = 1, MAJOR, MINOR, PATCH, variant] + checksum.
 */
#ifndef VERSION_H_
#define VERSION_H_

#define FIRMWARE_VERSION_MAJOR 1
#define FIRMWARE_VERSION_MINOR 7
#define FIRMWARE_VERSION_PATCH 0
#define FIRMWARE_VERSION_SCHEMA 1

#if FIRMWARE_VERSION_MAJOR > 15 || FIRMWARE_VERSION_MINOR > 15
#error "0xFD packs MAJOR and MINOR in a nibble each"
#endif

#endif /* VERSION_H_ */
