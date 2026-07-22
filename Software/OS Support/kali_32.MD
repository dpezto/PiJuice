# Installing PiJuice Software on Kali Linux 32-bit


https://www.offensive-security.com/kali-linux-arm-images/

1. Make sure you have the latest version of Kali Linux by running an update:
`sudo apt-get update`

2. Install the dependancies:
`sudo apt install i2c-tools lua5.3`

3. Add user `root` to I2C user group:
`sudo usermod -aG i2c root`

4. Add user `root` to pijuice user group:
`sudo visudo`
Add:
`root ALL=(pijuice) ALL`

5. Clone the PiJuice repository from GitHub:
`git clone https://github.com/PiSupply/PiJuice.git`

6. Install pijuice-base:
`cd PiJuice/Software/Install`
`dpkg -i pijuice-base_*_all.deb`

Fix broken packages:
`sudo apt --fix-broken install`

7. Reboot and run PiJuice CLI:
`sudo reboot`
Once rebooted run:
`pijuice_cli`

8. Once pijuice-base has been installed you can now install the GUI by running:
`dpkg -i pijuice-gui_*_all.deb`
Fix broken packages:
`sudo apt --fix-broken install`

"PiJuice Settings" will now be added to the application menu (Preferences). The
GUI is a GTK4 / libadwaita app that follows your desktop's light/dark theme
automatically. You can also launch it directly with:
`python3 /usr/bin/pijuice_gtk.py`

9. Reboot to start the tray icon and settings app:
`sudo reboot`
Once rebooted, open "PiJuice Settings" from the application menu.
