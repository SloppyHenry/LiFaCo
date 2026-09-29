#!/usr/bin/env bash
# Removes LiFaCo. With --purge the configuration, profiles and the group are deleted as well.
set -euo pipefail

PREFIX=/usr/local
STATE_DIR=/var/lib/fancontrol-linux
MANIFEST="$STATE_DIR/installed-files"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo ./uninstall.sh [--purge]" >&2
    exit 1
fi

# When stopped, the service hands the fans back to BIOS/firmware.
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

for b in fancontrol-linux fancontrol-linuxd fancontrol-linuxctl fancontrol-linux-upgrade lifaco lifacod lifacoctl lifaco-upgrade; do
    rm -f "$PREFIX/bin/$b"
done
rm -rf "$PREFIX/lib/fancontrol-linux" /opt/fancontrol-linux
rm -f "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
rm -f "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true

# Driver settings created by the installer.
REGEN=0
if [[ -f $MANIFEST ]]; then
    while read -r file; do
        [[ -n $file && -f $file ]] || continue
        [[ $file == *amdgpu* ]] && REGEN=1
        rm -f "$file"
        echo "Removed: $file"
    done < "$MANIFEST"
    rm -f "$MANIFEST"
fi
if [[ $REGEN -eq 1 ]]; then
    if command -v update-initramfs >/dev/null; then update-initramfs -u
    elif command -v dracut >/dev/null; then dracut -f
    elif command -v mkinitcpio >/dev/null; then mkinitcpio -P
    fi || echo "Note: please regenerate the initramfs so the AMD overdrive option is dropped."
fi
# Kernel parameters added by the installer (e.g. acpi_enforce_resources=lax).
if [[ -f $STATE_DIR/kernel-params ]]; then
    while read -r param; do
        [[ -n $param ]] || continue
        if command -v grubby >/dev/null; then
            grubby --update-kernel=ALL --remove-args="$param" || true
        elif [[ -f /etc/default/grub ]]; then
            sed -i -E "/^GRUB_CMDLINE_LINUX_DEFAULT=/ { s/ $param([\" ])/\1/; s/\"$param /\"/; s/\"$param\"/\"\"/ }" /etc/default/grub
            REGEN_GRUB=1
        elif command -v kernelstub >/dev/null; then
            kernelstub -d "$param" || true
        fi
        echo "Kernel parameter removed: $param (active after a reboot)"
    done < "$STATE_DIR/kernel-params"
    if [[ ${REGEN_GRUB:-0} -eq 1 ]]; then
        if command -v update-grub >/dev/null; then update-grub
        elif command -v grub2-mkconfig >/dev/null; then grub2-mkconfig -o "$( [[ -d /boot/grub2 ]] && echo /boot/grub2/grub.cfg || echo /boot/grub/grub.cfg )"
        elif command -v grub-mkconfig >/dev/null; then grub-mkconfig -o /boot/grub/grub.cfg
        fi || echo "Note: please regenerate the GRUB configuration."
    fi
    rm -f "$STATE_DIR/kernel-params" /etc/default/grub.fancontrol-linux.bak
fi
if [[ $(dkms status -m it87 2>/dev/null || true) == *it87* ]]; then
    echo "Note: the it87 driver installed via DKMS is kept, because the mainboard fans need it"
    echo "      (remove it: sudo dkms remove it87/<version> --all, see: dkms status it87)."
fi

if [[ $PURGE -eq 1 ]]; then
    rm -rf /etc/fancontrol-linux "$STATE_DIR"
    getent group fancontrol >/dev/null && { groupdel fancontrol 2>/dev/null || delgroup fancontrol; } || true
    echo "Configuration and group removed."
else
    echo "The configuration in /etc/fancontrol-linux is kept (remove everything: --purge)."
fi
echo "LiFaCo has been removed."
