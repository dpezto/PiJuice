/*******************************************************************************
# Author: Milan Neskovic, github.com/mmilann, Pi Supply, 2017-2021

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# Description: loads firmware into the PiJuice MCU through its built-in I2C
# bootloader (STM32 AN4221 protocol).
# Compile: cc -O2 -o pijuiceboot pijuiceboot.c
# Usage:   pijuiceboot <app address hex> <image> [i2c bus] [bootloader address hex]
# Example: pijuiceboot 14 ./PiJuice-V1.6_2021_09_10.elf.binary
#
# Exit codes (the UIs map 256 - code to a reason):
#   -1 cannot open the I2C bus      -6 cannot read the image
#   -2 cannot open the image        -7 page write failed
#   -3 bootloader did not answer    -8 page read-back failed
#   -4 first page erase failed      -9 page verify mismatch
#   -5 page erase failed           -10 jump to new code failed
********************************************************************************/

#include <errno.h>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define ACK 0x79
#define BOOTLOADER_ADDRESS 0x41
#define ERASE_PAGE_SIZE 2048
#define WRITE_PAGE_SIZE 256
#define FLASH_START_ADDRESS ((uint32_t)0x08000000)
#define MAX_ERASE_SECTORS 40

static int bootFd = -1;   /* the MCU's I2C bootloader */
static int appFd = -1;    /* the running firmware */

static int OpenSlave(const char *devicePath, int addr)
{
	int fd = open(devicePath, O_RDWR);
	if (fd < 0) {
		printf("Failed to open %s: %s\n", devicePath, strerror(errno));
		return -1;
	}
	if (ioctl(fd, I2C_SLAVE, addr) < 0) {
		printf("Failed to select slave 0x%02x on %s: %s\n", addr, devicePath, strerror(errno));
		close(fd);
		return -1;
	}
	return fd;
}

static uint8_t GetCheckSum(const uint8_t *msg, int size)
{
	uint8_t result = 0;
	for (int i = 0; i < size; i++)
		result ^= msg[i];
	return result;
}

/* Wait up to tries * step microseconds for one byte; 0 on ACK, <0 otherwise. */
static int WaitAck(int tries, int stepUs)
{
	uint8_t ack;
	for (int i = 0; i < tries; i++) {
		if (read(bootFd, &ack, 1) == 1) {
			if (ack == ACK)
				return 0;
			printf("no ack %x\n", ack);
			return -2;
		}
		usleep(stepUs);
	}
	return -1;
}

static int SendCommand(uint8_t cmd)
{
	uint8_t frame[2] = {cmd, (uint8_t)~cmd};
	return write(bootFd, frame, 2) == 2 ? 0 : -1;
}

static int SendAddress(uint32_t addr)
{
	uint8_t frame[5] = {addr >> 24, addr >> 16, addr >> 8, addr, 0};
	frame[4] = GetCheckSum(frame, 4);
	return write(bootFd, frame, 5) == 5 ? 0 : -1;
}

/* GET: returns the bootloader version, <0 when the bootloader is not answering. */
static int GetBootloaderVersion(void)
{
	uint8_t data[16];
	SendCommand(0x00);
	usleep(10000);
	if (WaitAck(200, 10000) < 0)
		return -1;
	int n = read(bootFd, data, 13);
	if (n < 1 || n < data[0])
		return -4;
	if (WaitAck(1, 0) < 0)
		return -5;
	return data[1];
}

static int ReadMemory(uint32_t addr, uint8_t *data, int32_t size)
{
	uint8_t frame[2];
	SendCommand(0x11);
	usleep(10000);
	if (WaitAck(1, 0) < 0)
		return -2;
	SendAddress(addr);
	usleep(500);
	if (WaitAck(1, 0) < 0)
		return -4;
	usleep(500);
	frame[0] = size - 1;
	frame[1] = ~frame[0];
	write(bootFd, frame, 2);
	usleep(500);
	if (WaitAck(200, 10000) < 0)
		return -6;
	if (read(bootFd, data, size) < size)
		return -7;
	return size;
}

static int WriteMemory(uint32_t addr, const uint8_t *data, int32_t size)
{
	uint8_t frame[WRITE_PAGE_SIZE + 2];
	SendCommand(0x31);
	usleep(500);
	if (WaitAck(1, 0) < 0)
		return -2;
	SendAddress(addr);
	usleep(500);
	if (WaitAck(1, 0) < 0)
		return -4;
	frame[0] = size - 1;
	memcpy(frame + 1, data, size);
	frame[size + 1] = GetCheckSum(frame, size + 1);
	write(bootFd, frame, size + 2);
	usleep(11000);
	if (WaitAck(200, 1000) < 0)
		return -6;
	return 0;
}

static int ExtendedEraseMemory(const uint16_t *pages, int32_t count)
{
	uint8_t frame[2 * MAX_ERASE_SECTORS + 3];
	SendCommand(0x44);
	usleep(10000);
	if (WaitAck(1, 0) < 0)
		return -2;
	uint16_t n = count - 1;
	frame[0] = n >> 8;
	frame[1] = n;
	frame[2] = frame[0] ^ frame[1];
	write(bootFd, frame, 3);
	usleep(1000);
	if (WaitAck(1, 0) < 0)
		return -4;
	for (int i = 0; i < count; i++) {
		frame[2 * i] = pages[i] >> 8;
		frame[2 * i + 1] = pages[i];
	}
	frame[2 * count] = GetCheckSum(frame, 2 * count);
	write(bootFd, frame, 2 * count + 1);
	usleep(count * 50000);
	if (WaitAck(200, 10000) < 0)
		return -6;
	return 0;
}

static int GoCommand(uint32_t addr)
{
	SendCommand(0x21);
	usleep(10000);
	if (WaitAck(1, 0) < 0)
		return -2;
	SendAddress(addr);
	usleep(10000);
	if (WaitAck(200, 10000) < 0)
		return -6;
	return 0;
}

int main(int argc, char *argv[])
{
	char devicePath[32] = "/dev/i2c-1";
	int appAddr, bootAddr = BOOTLOADER_ADDRESS;
	int ret = 0;
	FILE *f = NULL;

	if (argc < 3 || argc > 5) {
		printf("Usage: %s <app address hex> <image> [i2c bus] [bootloader address hex]\n", argv[0]);
		printf("Example: %s 14 ./PiJuice-V1.6_2021_09_10.elf.binary\n", argv[0]);
		return -1;
	}
	appAddr = (int)strtol(argv[1], NULL, 16);
	if (argc >= 4)
		snprintf(devicePath, sizeof devicePath, "/dev/i2c-%d", atoi(argv[3]));
	if (argc >= 5)
		bootAddr = (int)strtol(argv[4], NULL, 16);
	printf("app 0x%02x, bootloader 0x%02x on %s\n", appAddr, bootAddr, devicePath);

	bootFd = OpenSlave(devicePath, bootAddr);
	appFd = OpenSlave(devicePath, appAddr);
	if (bootFd < 0 || appFd < 0) {
		printf("Error opening I2C ports, aborting\n");
		ret = -1;
		goto end;
	}

	printf("Input file %s\n", argv[2]);
	f = fopen(argv[2], "rb");
	if (!f) {
		printf("Unable to open input file!\n");
		ret = -2;
		goto end;
	}
	fseek(f, 0L, SEEK_END);
	long fSize = ftell(f);
	rewind(f);

	/* Ask the running firmware to jump into the bootloader; the 2-byte form
	 * is what firmware before 1.1 understood. */
	printf("Starting bootloader\n");
	uint8_t startCmd[] = {0xFE, 0x01, 0xFE};
	write(appFd, startCmd, 3);
	usleep(10000);
	write(appFd, startCmd, 2);
	usleep(10000);

	int version = GetBootloaderVersion();
	if (version < 0) {
		printf("error receiving data %d\n", version);
		ret = -3;
		goto end;
	}
	printf("bootloader version: %x\n", version);

	int32_t erasePageCount = (fSize + ERASE_PAGE_SIZE - 1) / ERASE_PAGE_SIZE;
	printf("erase page count %d\n", erasePageCount);

	uint16_t pages[MAX_ERASE_SECTORS] = {0};
	int n = ExtendedEraseMemory(pages, 1);
	if (n < 0) {
		printf("erase error %d\n", n);
		ret = -4;
		goto end;
	}
	printf("first page erase success\n");

	for (int erased = 1; erased < erasePageCount; erased += MAX_ERASE_SECTORS) {
		int end = erased + MAX_ERASE_SECTORS;
		if (end > erasePageCount)
			end = erasePageCount;
		for (int i = erased; i < end; i++)
			pages[i - erased] = i;
		n = ExtendedEraseMemory(pages, end - erased);
		if (n < 0) {
			printf("erase error %d\n", n);
			ret = -5;
			goto end;
		}
	}
	printf("Erase success\n");

	/* Pages go in from the last one down to page 0 (the vector table), so an
	 * interrupted write leaves nothing bootable: recover with SW3 held at
	 * power-up, which enters this bootloader without the firmware's help. */
	int32_t pageCount = (fSize + WRITE_PAGE_SIZE - 1) / WRITE_PAGE_SIZE;
	printf("page count %d\n", pageCount);
	uint8_t pageData[WRITE_PAGE_SIZE], readData[WRITE_PAGE_SIZE];
	for (int i = pageCount - 1; i >= 0; i--) {
		memset(pageData, 0xFF, WRITE_PAGE_SIZE);
		fseek(f, (long)i * WRITE_PAGE_SIZE, SEEK_SET);
		fread(pageData, 1, WRITE_PAGE_SIZE, f);
		if (ferror(f)) {
			printf("Error reading input file!\n");
			ret = -6;
			goto end;
		}
		uint32_t addr = FLASH_START_ADDRESS + (uint32_t)i * WRITE_PAGE_SIZE;
		int status = WriteMemory(addr, pageData, WRITE_PAGE_SIZE);
		if (status < 0) {
			printf("Error writing page %d: %d\n", i, status);
			ret = -7;
			goto end;
		}
		status = ReadMemory(addr, readData, WRITE_PAGE_SIZE);
		if (status < 0) {
			printf("error reading %d %d\n", i, status);
			ret = -8;
			goto end;
		}
		if (memcmp(pageData, readData, WRITE_PAGE_SIZE) != 0) {
			printf("verify failed %d\n", i);
			ret = -9;
			goto end;
		}
		printf("Page %d programmed successfully\n", i);
	}
	printf("Flash programming finished successfully\n");

	usleep(10000);
	n = GoCommand(FLASH_START_ADDRESS);
	if (n < 0) {
		printf("Cannot execute code %d\n", n);
		ret = -10;
		goto end;
	}
	printf("Code executed successfully\n");

end:
	if (f)
		fclose(f);
	if (bootFd >= 0)
		close(bootFd);
	if (appFd >= 0)
		close(appFd);
	return ret;
}
