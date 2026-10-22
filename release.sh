#!/bin/bash
# Cut a release from the Conventional Commits since the last vX.Y.Z tag:
# next version (git-cliff, see cliff.toml), CHANGELOG.md, both Debian
# changelogs, __version__ in pijuice.py, the GUI's dependency on pijuice-base,
# a chore(release) commit and an annotated tag. Nothing is pushed.
#
#   ./release.sh              version from the commits: feat -> minor,
#                             fix/perf/... -> patch, breaking -> major
#   ./release.sh 1.12.0       explicit version
#   ./release.sh --dry-run    print what would be written, change nothing
set -euo pipefail
cd "$(dirname "$0")"

SRC=Software/Source
# Commits touching only these go to pijuice-gui; everything else to pijuice-base.
GUI_PATHS=("$SRC/src/pijuice_gtk.py" "$SRC/src/pijuice_tray.py" "$SRC/debian-gui/**"
           "$SRC/data/pijuice-gtk.desktop" "$SRC/data/pijuice-tray.desktop" "$SRC/data/images/**")
DEB_BODY='{% for commit in commits %}  * {% if commit.scope %}{{ commit.scope }}: {% endif %}{{ commit.message | split(pat="\n") | first | trim }}
{% endfor %}'

die() { echo "release: $*" >&2; exit 1; }

DRY=false
if [ "${1:-}" = --dry-run ]; then DRY=true; shift; fi
command -v git-cliff > /dev/null || die "git-cliff is not installed"
[ "$(git branch --show-current)" = master ] || die "not on master"
$DRY || [ -z "$(git status --porcelain)" ] || die "working tree not clean"
git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' > /dev/null 2>&1 \
	|| die "no vX.Y.Z tag to release from"

UNRELEASED=$(git cliff --unreleased 2> /dev/null) || die "git-cliff failed; run: git cliff --unreleased"
grep -q '^- ' <<< "$UNRELEASED" \
	|| die "nothing to release: no feat/fix/perf/refactor/docs/build/test commits since the last tag"

VERSION=${1:-$(git cliff --bumped-version 2> /dev/null)}
VERSION=${VERSION#v}
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a semver version: $VERSION"
! git rev-parse -q --verify "refs/tags/v$VERSION" > /dev/null || die "tag v$VERSION already exists"

# Debian changelog bullets for the unreleased commits matching the path args,
# wrapped at 78 columns with the continuation indented under the text.
deb_items() {
	git cliff --unreleased --tag "v$VERSION" --body "$DEB_BODY" "$@" 2> /dev/null | awk '
		substr($0, 1, 4) != "  * " { if (NF) print; next }
		{
			n = split(substr($0, 5), w, " "); line = "  * "; fresh = 1
			for (i = 1; i <= n; i++) {
				if (!fresh && length(line) + 1 + length(w[i]) > 78) { print line; line = "    "; fresh = 1 }
				line = line (fresh ? "" : " ") w[i]; fresh = 0
			}
			print line
		}'
}

MAINT="$(git config user.name) <$(git config user.email)>"
DATE=$(date -R)
deb_entry() {  # PKG URGENCY GIT-CLIFF-PATH-ARGS...
	local pkg=$1 urgency=$2 items
	shift 2
	items=$(deb_items "$@")
	[ -n "$items" ] || items="  * No changes in this package; released with pijuice-base $VERSION."
	printf '%s (%s) unstable; urgency=%s\n\n%s\n\n -- %s  %s\n\n' "$pkg" "$VERSION" "$urgency" "$items" "$MAINT" "$DATE"
}

EXCLUDE=() INCLUDE=()
for path in "${GUI_PATHS[@]}"; do EXCLUDE+=(--exclude-path "$path"); INCLUDE+=(--include-path "$path"); done
BASE_ENTRY=$(deb_entry pijuice-base low "${EXCLUDE[@]}")
GUI_ENTRY=$(deb_entry pijuice-gui low "${INCLUDE[@]}")

if $DRY; then
	git cliff --unreleased --tag "v$VERSION" 2> /dev/null
	printf '\n--- debian-base/changelog\n%s\n\n--- debian-gui/changelog\n%s\n' "$BASE_ENTRY" "$GUI_ENTRY"
	exit 0
fi

prepend() {  # FILE TEXT
	local tmp
	tmp=$(mktemp)
	{ printf '%s\n\n' "$2"; cat "$1"; } > "$tmp"
	mv "$tmp" "$1"
}
prepend CHANGELOG.md "$(git cliff --unreleased --tag "v$VERSION" 2> /dev/null | sed '/./,$!d')"
prepend "$SRC/debian-base/changelog" "$BASE_ENTRY"
prepend "$SRC/debian-gui/changelog" "$GUI_ENTRY"
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" "$SRC/pijuice.py"
# Both packages ship together; the GUI imports pijuice_service from this base.
sed -i "s/pijuice-base (>= [^)]*)/pijuice-base (>= $VERSION)/" "$SRC/debian-gui/control"

git add CHANGELOG.md "$SRC/debian-base/changelog" "$SRC/debian-gui/changelog" "$SRC/pijuice.py" "$SRC/debian-gui/control"
git commit -q -m "chore(release): pijuice-base and pijuice-gui $VERSION"
git tag -a "v$VERSION" -m "pijuice $VERSION"
echo "Released v$VERSION. Build on the Pi with $SRC/pckg-pijuice.sh; push with: git push origin master v$VERSION"
