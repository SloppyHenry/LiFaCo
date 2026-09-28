#!/usr/bin/env bash
# Läuft im Debian-13-Container: Pakete + Abhängigkeiten ins AppDir kopieren, Programm dazu, AppImage bauen.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-1 libadwaita-1-0 librsvg2-common \
adwaita-icon-theme hicolor-icon-theme liquidctl shared-mime-info libglib2.0-bin fonts-dejavu-core"
# Grundsystem-Pakete, die zur Laufzeit nicht gebraucht werden (Paketverwaltung, Shells, Dienste, Schriften).
EXCLUDE='^(dpkg|debconf|debianutils|perl|perl-base|perl-modules-.*|libperl.*|bash|dash|coreutils|login|passwd|adduser|base-files|base-passwd|sensible-utils|init-system-helpers|systemd|systemd-.*|udev|dbus|dbus-bin|dbus-daemon|dbus-system-bus-common|dbus-session-bus-common|dbus-user-session|dconf-gsettings-backend|dconf-service|fonts-.*|tzdata|ucf|mount|util-linux|util-linux-extra|libc-bin|sed|grep|findutils|tar|gzip|hostname|ncurses-base|ncurses-bin|python3-pip|.*-doc|gsettings-desktop-schemas)$'

apt-get update -qq
# shellcheck disable=SC2086
apt-get install -y -qq --no-install-recommends $PKGS wget ca-certificates file desktop-file-utils >/dev/null

APPDIR=/tmp/AppDir
rm -rf "${APPDIR:?}" && mkdir -p "$APPDIR"
# shellcheck disable=SC2086
closure=$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces \
    --no-enhances $PKGS | grep -E '^[a-z0-9]' | sort -u)
for p in $closure; do
    [[ $p =~ $EXCLUDE ]] && continue
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || continue
    dpkg -L "$p" | while read -r f; do
        if [[ ( -f $f || -L $f ) && ! -d $f ]]; then echo "$f"; fi
    done
done | sort -u > /tmp/files
echo "Pakete: $(wc -w <<<"$closure"), Dateien: $(wc -l < /tmp/files)"
tar -cf - -T /tmp/files 2>/dev/null | tar -xf - -C "$APPDIR"

# Zusammenführen von /lib und /usr/lib (merged /usr), damit nur ein Bibliothekspfad nötig ist.
if [[ -d ${APPDIR:?}/lib ]]; then cp -an "${APPDIR:?}/lib/." "${APPDIR:?}/usr/lib/" && rm -rf "${APPDIR:?}/lib"; fi

# Zur Installationszeit erzeugte Caches ergänzen.
LIBDIR=usr/lib/x86_64-linux-gnu
cp /$LIBDIR/gdk-pixbuf-2.0/2.10.0/loaders.cache "$APPDIR/$LIBDIR/gdk-pixbuf-2.0/2.10.0/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
update-mime-database "$APPDIR/usr/share/mime" >/dev/null 2>&1 || true
gtk4-update-icon-cache -q -t -f "$APPDIR/usr/share/icons/Adwaita" 2>/dev/null || true

# Ersatzschrift für Systeme ohne passende Schriften (die Schriften des Systems haben Vorrang).
mkdir -p "$APPDIR/usr/share/fonts/fancontrol"
cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
    "$APPDIR/usr/share/fonts/fancontrol/"

# Unnötiges entfernen.
rm -rf "$APPDIR"/usr/share/{doc,man,info,lintian,bug,bash-completion,zsh}
find "$APPDIR/usr/share/locale" -mindepth 1 -maxdepth 1 ! -name 'de*' ! -name 'en*' -exec rm -rf {} + 2>/dev/null || true
PYLIB=$(ls -d "$APPDIR"/usr/lib/python3.[0-9]*)
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$PYLIB"/config-*

# Programm.
APP="$APPDIR/usr/lib/fancontrol-linux"
mkdir -p "$APP"
cp -r /src/fancontrol_linux /src/bin /src/data /src/install.sh /src/uninstall.sh "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
python3 -m compileall -q -j0 "$PYLIB" "$APPDIR/usr/lib/python3/dist-packages" "$APP/fancontrol_linux" >/dev/null || true

install -m 755 /src/packaging/appimage/AppRun "$APPDIR/AppRun"
sed 's/^Exec=.*/Exec=fancontrol-linux/' /src/data/io.github.fancontrol_linux.desktop > "$APPDIR/io.github.fancontrol_linux.desktop"
echo "X-AppImage-Name=FanControl" >> "$APPDIR/io.github.fancontrol_linux.desktop"
cp /src/data/io.github.fancontrol_linux.svg "$APPDIR/io.github.fancontrol_linux.svg"
cp /src/data/io.github.fancontrol_linux.svg "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/metainfo"

# Selbsttest mit dem mitgelieferten Loader.
"$APPDIR/AppRun" --python -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw; import liquidctl; print("Selbsttest: GTK", Gtk.get_minor_version(), "Adw", Adw.get_minor_version())'

cd /tmp
wget -q https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x appimagetool-x86_64.AppImage
ARCH=x86_64 ./appimagetool-x86_64.AppImage --appimage-extract-and-run -n "$APPDIR" /out/FanControl-x86_64.AppImage 2>&1 | tail -3
chown "${HOST_UID:-0}:${HOST_GID:-0}" /out/FanControl-x86_64.AppImage
ls -lh /out/FanControl-x86_64.AppImage
