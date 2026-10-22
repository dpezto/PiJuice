/* Host test: make test. APA rows from the LC709203F datasheet, Table 7 (Type-01). */
#include <assert.h>
#include <stdio.h>
#include "fuel_gauge_apa.h"

int main(void) {
	assert(FuelGaugeApa(100) == 0x08 && FuelGaugeApa(200) == 0x0B && FuelGaugeApa(500) == 0x10);
	assert(FuelGaugeApa(1000) == 0x19 && FuelGaugeApa(2000) == 0x2D && FuelGaugeApa(3000) == 0x36);
	assert(FuelGaugeApa(1600) == 0x25);                 /* typhon's custom 1600 mAh profile */
	assert(FuelGaugeApa(50) == 0x08);                   /* below the table: first row */
	assert(FuelGaugeApa(12000) == 0x36 && FuelGaugeApa(0xFFFFFFFF) == 0x36); /* past it: as before */
	puts("fuel_gauge_apa ok");
	return 0;
}
