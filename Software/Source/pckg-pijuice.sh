#!/bin/bash
# Build pijuice-base and pijuice-gui .debs with plain dpkg-deb.
#
# Replaces the stdeb/distutils/debhelper pipeline, which no longer runs on
# Debian 13: distutils is gone since Python 3.12, stdeb crashes on 3.13, and
# debian/rules called dh_systemd_* (removed in debhelper 13). This script is
# the only build path; the install layout is defined here.
# Output: deb_dist/pijuice-{base,gui}_<version>_all.deb
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' pijuice.py)
MOD_VER=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' ../kernel/pijuice_power/dkms.conf)
[ -n "$VERSION" ] || { echo "cannot read __version__ from pijuice.py" >&2; exit 1; }
[ -n "$MOD_VER" ] || { echo "cannot read PACKAGE_VERSION from dkms.conf" >&2; exit 1; }
for pkg in base gui; do
	head -1 "debian-$pkg/changelog" | grep -q "($VERSION)" \
		|| { echo "debian-$pkg/changelog has no ($VERSION) entry on top" >&2; exit 1; }
done
grep -q "PJ_MOD_VER=$MOD_VER\$" debian-base/postinst \
	|| { echo "debian-base/postinst PJ_MOD_VER does not match dkms.conf ($MOD_VER)" >&2; exit 1; }

OUT=deb_dist
rm -rf "$OUT"
mkdir -p "$OUT"

# put ROOT DIR MODE FILE...  (install files into ROOT/DIR)
put() {
	local root=$1 dir=$2 mode=$3
	shift 3
	install -d "$root$dir"
	install -m "$mode" "$@" "$root$dir/"
}

# build PKG SRCDIR CONFFILE...
build() {
	local pkg=$1 src=$2 root=$OUT/$1
	shift 2
	local maint
	maint=$(sed -n 's/^ -- \(.*>\)  .*/\1/p' "$src/changelog" | head -1)

	install -d "$root/usr/share/doc/$pkg"
	gzip -9n < "$src/changelog" > "$root/usr/share/doc/$pkg/changelog.gz"
	chmod 644 "$root/usr/share/doc/$pkg/changelog.gz"

	install -d "$root/DEBIAN"
	for s in preinst postinst prerm postrm; do
		[ -f "$src/$s" ] || continue
		grep -v '^#DEBHELPER#$' "$src/$s" > "$root/DEBIAN/$s"
		chmod 755 "$root/DEBIAN/$s"
	done
	printf '%s\n' "$@" > "$root/DEBIAN/conffiles"
	chmod 644 "$root/DEBIAN/conffiles"

	(cd "$root" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' | sort -z | xargs -0 md5sum) > "$root/DEBIAN/md5sums"
	{
		echo "Package: $pkg"
		echo "Version: $VERSION"
		echo "Architecture: all"
		echo "Maintainer: $maint"
		sed -n 's/^\(Section\|Priority\): /&/p' "$src/control"
		echo "Installed-Size: $(du -sk --exclude=DEBIAN "$root" | cut -f1)"
		# Binary stanza minus the fields written above.
		awk '/^Package:/{b=1; next} b && !/^Architecture:/' "$src/control"
	} > "$root/DEBIAN/control"

	dpkg-deb --root-owner-group --build "$root" "$OUT/${pkg}_${VERSION}_all.deb"
}

B=$OUT/pijuice-base
put $B /usr/bin 755 src/pijuice_sys.py src/pijuice_log.py
put $B /usr/bin 644 src/pijuice_cli.py
put $B /usr/bin 755 bin/pijuiceboot32 bin/pijuiceboot64 bin/pijuice_cli32 bin/pijuice_cli64
put $B /usr/lib/python3/dist-packages 644 pijuice.py pijuice_service.py pijuice_battery.py
put $B /usr/share/pijuice/data/firmware 644 data/firmware/*
put $B /etc/udev/rules.d 644 data/99-i2c.rules
put $B /etc/sudoers.d 440 data/020_pijuice-nopasswd
# Same path as pijuice-base 1.8: moving /lib -> /usr/lib inside one package loses the file on merged-/usr systems.
put $B /lib/systemd/system 644 debian-base/pijuice.service
put $B "/usr/src/pijuice-power-$MOD_VER" 644 ../kernel/pijuice_power/pijuice_power.c \
	../kernel/pijuice_power/Makefile ../kernel/pijuice_power/dkms.conf
put $B /etc/modules-load.d 644 ../kernel/pijuice_power/pijuice_power.conf
install -D -m 644 ../kernel/pijuice_power/pijuice_power-modprobe.conf $B/etc/modprobe.d/pijuice_power.conf
build pijuice-base debian-base \
	/etc/udev/rules.d/99-i2c.rules /etc/sudoers.d/020_pijuice-nopasswd /etc/modules-load.d/pijuice_power.conf \
	/etc/modprobe.d/pijuice_power.conf

G=$OUT/pijuice-gui
put $G /usr/bin 755 src/pijuice_tray.py src/pijuice_gtk.py
put $G /usr/share/applications 644 data/pijuice-gtk.desktop
put $G /etc/xdg/autostart 644 data/pijuice-tray.desktop
put $G /usr/share/pijuice/data/images 644 data/images/*
# Also in pixmaps so the panel's GtkIconTheme resolves tray icons by bare name.
put $G /usr/share/pixmaps 644 data/images/*.png
build pijuice-gui debian-gui /etc/xdg/autostart/pijuice-tray.desktop

ls -l "$OUT"/*.deb
