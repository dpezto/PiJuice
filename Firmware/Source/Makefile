# PiJuice HAT firmware (STM32F030CC) with arm-none-eabi-gcc, outside STM32CubeIDE.
# Mirrors the CubeIDE "Debug" configuration the released images were built
# with (-Ofast, DEBUG, LOGGING; FreeRTOS off: the define is xRTOS_FREERTOS).
#
#   make            build/PiJuice.elf, .hex, .bin and .map
#   make release    also PiJuice-V<version>_<date>.elf.binary next to the old images
#   make size       flash/RAM use
#
# The sources and the link order follow STM32CubeIDE/.project.

VERSION := $(shell sed -n 's/^\#define FIRMWARE_VERSION_\(MAJOR\|MINOR\|PATCH\) *\([0-9]*\).*/\2/p' Inc/version.h 2>/dev/null | paste -sd.)
DATE    := $(shell date +%Y_%m_%d)

CROSS   ?= arm-none-eabi-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
SIZE    := $(CROSS)size
BUILD   := build

APP := analog battery button charger_bq2416x command_server config_switch_resistor crc8_atm \
       eeprom fuel_gauge_lc709203f io_control led load_current_sense logging main nv \
       power_management power_source rtc_ds1339_emu stm32f0xx_hal_msp stm32f0xx_it time_count
HAL := hal hal_adc hal_adc_ex hal_cortex hal_dma hal_exti hal_flash hal_flash_ex hal_gpio \
       hal_i2c hal_i2c_ex hal_iwdg hal_pwr hal_pwr_ex hal_rcc hal_rcc_ex hal_rtc hal_rtc_ex \
       hal_tim hal_tim_ex hal_wwdg

SRC := $(APP:%=Src/%.c) Core/Src/system_stm32f0xx.c \
       $(HAL:%=Drivers/STM32F0xx_HAL_Driver/Src/stm32f0xx_%.c) \
       STM32CubeIDE/Src/syscalls.c STM32CubeIDE/Src/sysmem.c
ASM := STM32CubeIDE/startup_stm32f030cctx.s
OBJ := $(SRC:%.c=$(BUILD)/%.o) $(ASM:%.s=$(BUILD)/%.o)

# GCC >= 14 makes these errors; the 2018 toolchain only warned. Kept visible as warnings.
LEGACY  := -Wno-error=implicit-function-declaration -Wno-error=int-conversion \
           -Wno-error=incompatible-pointer-types
MCU     := -mcpu=cortex-m0 -mthumb -mfloat-abi=soft
DEFS    := -DUSE_HAL_DRIVER -DSTM32F030xC -DDEBUG -DLOGGING
INCS    := -IInc -IDrivers/CMSIS/Include -IDrivers/CMSIS/Device/ST/STM32F0xx/Include \
           -IDrivers/STM32F0xx_HAL_Driver/Inc -IDrivers/STM32F0xx_HAL_Driver/Inc/Legacy
CFLAGS  := $(MCU) $(DEFS) $(INCS) -std=gnu11 -Ofast -g3 -Wall -ffunction-sections -fdata-sections \
           --specs=nano.specs -MMD -MP $(LEGACY)
LDFLAGS := $(MCU) -TSTM32CubeIDE/STM32F030CCTX_FLASH.ld --specs=nosys.specs --specs=nano.specs \
           -Wl,--gc-sections -static -Wl,-Map=$(BUILD)/PiJuice.map -Wl,--print-memory-usage \
           -Wl,--start-group -lc -lm -Wl,--end-group

all: $(BUILD)/PiJuice.hex $(BUILD)/PiJuice.bin

$(BUILD)/PiJuice.elf: $(OBJ)
	$(CC) $(OBJ) $(LDFLAGS) -o $@

$(BUILD)/%.o: %.c
	@mkdir -p $(dir $@)
	@echo CC $<
	@$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/%.o: %.s
	@mkdir -p $(dir $@)
	$(CC) $(MCU) -x assembler-with-cpp -c $< -o $@

$(BUILD)/PiJuice.hex: $(BUILD)/PiJuice.elf
	$(OBJCOPY) -O ihex $< $@

$(BUILD)/PiJuice.bin: $(BUILD)/PiJuice.elf
	$(OBJCOPY) -O binary $< $@

release: all
	cp $(BUILD)/PiJuice.bin ../PiJuice-V$(VERSION)_$(DATE).elf.binary
	cp $(BUILD)/PiJuice.hex ../PiJuice-V$(VERSION)_$(DATE).elf.hex
	@echo ../PiJuice-V$(VERSION)_$(DATE).elf.binary

size: $(BUILD)/PiJuice.elf
	$(SIZE) $<

clean:
	rm -rf $(BUILD)

.PHONY: all release size clean
-include $(OBJ:.o=.d)

# CubeIDE's sysmem.c predates newlib dropping caddr_t.
$(BUILD)/STM32CubeIDE/Src/sysmem.o: CFLAGS += -Dcaddr_t=char*
