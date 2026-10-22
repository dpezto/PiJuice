# PiJuice firmware

There are two ways in which you can upgrade the firmware for the PiJuice. You can use the command line tool provided here or refer to the [Software section](https://github.com/PiSupply/PiJuice/tree/master/Software) to find out how to perform a firmware update via the GUI or CLI.

### Usage:
```text
pijuiceboot i2c_address path_to_firmware_binary [i2c_bus] [bootloader_address]
```
`i2c_bus` defaults to 1, `bootloader_address` to 41 (hex). The binaries in
`Software/Source/bin/` are built from `pijuiceboot.c` with:
```bash
gcc -O2 -o pijuiceboot64 pijuiceboot.c   # on 64-bit Pi OS
gcc -O2 -o pijuiceboot32 pijuiceboot.c   # natively on 32-bit Pi OS (Raspbian armhf)
```
Build `pijuiceboot32` on Raspbian itself: its toolchain defaults to ARMv6 +
VFP. A Debian `arm-linux-gnueabihf` cross build links Thumb-2 startup code
(`_start`) even with `-march=armv6`, and crashes on a Pi Zero / Zero W / 1
before `main`. Check with `readelf -A pijuiceboot32` (`Tag_CPU_arch: v6`,
`Tag_THUMB_ISA_use: Thumb-1`). Both binaries need glibc >= 2.34.

### Example:
```bash
pijuiceboot 14 PiJuice-Vx.y_YYYY_MM_DD.elf.binary
```

The firmware source code is available in this directory but it requires deep understanding of the hardware design and the programming involved if you wanted to customise it. Making changes to the firmware may even harm components or damage the battery.
The majority of customisation can anyway be done by using the I2C command interface provided through the Python API that is installed together with configuration GUI. This is the best approach for end users wishing to customise further PiJuice for their needs.

## PiJuice bricked after unsucsessful firmware update

To recover from this issue follow these steps:

1. Make sure that you Raspberry Pi is shutdown
2. Disconnect all power to the PiJuice or Raspberry Pi and remove the PiJuice battery
3. Hold down SW3 button on the PiJuice and connect power to the Raspberry Pi
4. Release SW3 button after few seconds to enter bootloader mode\
**NOTE: LEDs on the PiJuice will remain off**
5. Stop the pijuice service with `sudo systemctl stop pijuice.service`
6. Download the Github repository `sudo git clone https://github.com/PiSupply/PiJuice.git`
7. Enter the Firmware directory `cd PiJuice/Firmware/`
8. Run `pijuiceboot 14 PiJuice-Vx.y_YYYY_MM_DD.elf.binary`
9. Wait until the Firmware has successfully flashed
10. Shutdown the Raspberry Pi and remove power
11. Connect the battery to the PiJuice and boot up as normal
