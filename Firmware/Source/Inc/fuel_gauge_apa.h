#ifndef FUEL_GAUGE_APA_H_
#define FUEL_GAUGE_APA_H_

#include <stdint.h>

// LC709203F datasheet Table 7, typical APA for Type-01 (3.7 V / 4.2 V, the
// profile 0x12 = 1 selects) by design capacity per cell. Interpolated between
// rows; past 3000 mAh the table ends and the fixed 0x36 used before is kept.
// It was 0x36 (3000 mAh) for every battery: a 1600 mAh pack wants ~0x25.
static inline uint16_t FuelGaugeApa(uint32_t capacity) {
	static const uint16_t mAh[] = {100, 200, 500, 1000, 2000, 3000};
	static const uint8_t apa[] = {0x08, 0x0B, 0x10, 0x19, 0x2D, 0x36};
	uint8_t i;
	if (capacity <= mAh[0]) return apa[0];
	for (i = 1; i < sizeof(mAh)/sizeof(mAh[0]); i++) {
		if (capacity <= mAh[i]) {
			return apa[i-1] + (uint32_t)(apa[i] - apa[i-1]) * (capacity - mAh[i-1]) / (mAh[i] - mAh[i-1]);
		}
	}
	return apa[i-1];
}

#endif /* FUEL_GAUGE_APA_H_ */
