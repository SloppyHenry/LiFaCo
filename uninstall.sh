#!/usr/bin/env bash
# Entfernt FanControl for Linux. Mit --purge werden auch Konfiguration, Profile und die Gruppe gelöscht.
set -euo pipefail

PREFIX=/usr/local
STATE_DIR=/var/lib/fancontrol-linux
MANIFEST="$STATE_DIR/installed-files"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausführen: sudo ./uninstall.sh [--purge]" >&2
    exit 1
fi

# Beim Stoppen gibt der Dienst die Lüfter an BIOS/Firmware zurück.
if [[ -f /etc/systemd/system/fancontrol-linux.service ]]; then
    systemctl disable --now fancontrol-linux.service 2>/dev/null || true
    rm -f /etc/systemd/system/fancontrol-linux.service
    systemctl daemon-reload 2>/dev/null || true
fi
if [[ -f /etc/init.d/fancontrol-linux ]]; then
    rc-service fancontrol-linux stop 2>/dev/null || true
    rc-update del fancontrol-linux default 2>/dev/null || true
    rm -f /etc/init.d/fancontrol-linux
fi
if [[ -d /etc/sv/fancontrol-linux ]]; then
    sv stop fancontrol-linux 2>/dev/null || true
    rm -f /var/service/fancontrol-linux /etc/runit/runsvdir/default/fancontrol-linux
    rm -rf /etc/sv/fancontrol-linux
fi

for b in fancontrol-linux fancontrol-linuxd fancontrol-linuxctl; do
    rm -f "$PREFIX/bin/$b"
done
rm -rf "$PREFIX/lib/fancontrol-linux"
rm -f "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
rm -f "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true

# Treiber-Einstellungen, die der Installer angelegt hat.
REGEN=0
if [[ -f $MANIFEST ]]; then
    while read -r file; do
        [[ -n $file && -f $file ]] || continue
        [[ $file == *amdgpu* ]] && REGEN=1
        rm -f "$file"
        echo "Entfernt: $file"
    done < "$MANIFEST"
    rm -f "$MANIFEST"
fi
if [[ $REGEN -eq 1 ]]; then
    if command -v update-initramfs >/dev/null; then update-initramfs -u
    elif command -v dracut >/dev/null; then dracut -f
    elif command -v mkinitcpio >/dev/null; then mkinitcpio -P
    fi || echo "Hinweis: initramfs bitte neu erzeugen, damit die AMD-Overdrive-Option entfällt."
fi
if [[ -d /usr/src/it87-fancontrol-linux ]]; then
    echo "Hinweis: Der per DKMS installierte it87-Treiber bleibt erhalten (entfernen: sudo dkms remove it87/<version> --all)."
fi

if [[ $PURGE -eq 1 ]]; then
    rm -rf /etc/fancontrol-linux "$STATE_DIR"
    getent group fancontrol >/dev/null && { groupdel fancontrol 2>/dev/null || delgroup fancontrol; } || true
    echo "Konfiguration und Gruppe entfernt."
else
    echo "Konfiguration unter /etc/fancontrol-linux bleibt erhalten (vollständig entfernen: --purge)."
fi
echo "FanControl for Linux wurde entfernt."
