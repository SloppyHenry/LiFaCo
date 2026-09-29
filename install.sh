#!/usr/bin/env bash
# LiFaCo – installer for Debian/Ubuntu/Mint, Fedora/RHEL, Arch/Manjaro, openSUSE, Void, Alpine, Gentoo.
# Installs the program, dependencies, hardware drivers and the background service (systemd, OpenRC or runit).
set -euo pipefail
# Note: never pipe into "grep -q" here – it exits on the first match, the writer gets SIGPIPE (141)
# and pipefail turns a match into a failure. Use "grep ... >/dev/null" instead.

PREFIX=/usr/local
LIBDIR="$PREFIX/lib/fancontrol-linux"
STATE_DIR=/var/lib/fancontrol-linux
MANIFEST="$STATE_DIR/installed-files"
LOG=/var/log/fancontrol-linux-install.log
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_SERVICE=1
INSTALL_DEPS=1
SETUP_HARDWARE=1
ASSUME_YES=0
AMD_OVERDRIVE=ask
IT87_DKMS=ask
THINKPAD=ask

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options]

  -y, --yes            No questions; safe default answers (no kernel/boot changes)
  --no-deps            Do not install packages
  --no-hardware        Do not detect/load drivers (sensors-detect, vendor modules)
  --no-service         Do not set up the background service
  --amd-overdrive      AMD RDNA3/4: enable overdrive for fan control without asking
  --it87-dkms          Install the current it87 driver (ITE chips) via DKMS without asking
  --thinkpad-fan       ThinkPad: enable fan control (thinkpad_acpi fan_control=1) without asking
  -h, --help           This help

From the AppImage: sudo ./LiFaCo-x86_64.AppImage --install [options]
EOF
}

APPIMAGE_SRC=""
OPT=/opt/fancontrol-linux
while [[ $# -gt 0 ]]; do
    arg=$1
    shift
    case "$arg" in
        --appimage) APPIMAGE_SRC=${1:?--appimage needs a directory}; shift ;;
        -y|--yes) ASSUME_YES=1 ;;
        --no-deps) INSTALL_DEPS=0 ;;
        --no-hardware) SETUP_HARDWARE=0 ;;
        --no-service) WITH_SERVICE=0 ;;
        --amd-overdrive) AMD_OVERDRIVE=yes ;;
        --it87-dkms) IT87_DKMS=yes ;;
        --thinkpad-fan) THINKPAD=yes ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo ./install.sh" >&2
    exit 1
fi

# --- Output -------------------------------------------------------------------
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
step() { echo; echo "${B}==> $*${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
fail() { echo "  ${R}✗${N} $*"; }
HINTS=()
hint() { HINTS+=("$*"); warn "$*"; }

# ask "question" default(y/n) → 0 = yes
ask() {
    local question=$1 default=$2 answer
    if [[ $ASSUME_YES -eq 1 || ! -t 0 ]]; then
        [[ $default == y ]]
        return
    fi
    local suffix="[y/N]"; [[ $default == y ]] && suffix="[Y/n]"
    read -r -p "  ? $question $suffix " answer || answer=
    answer=${answer,,}
    [[ -z $answer ]] && answer=$default
    [[ $answer == j* || $answer == y* ]]
}

record() { mkdir -p "$STATE_DIR"; grep -qxF "$1" "$MANIFEST" 2>/dev/null || echo "$1" >> "$MANIFEST"; }
: > "$LOG"

# --- Detect distribution -------------------------------------------------------
. /etc/os-release 2>/dev/null || true
DISTRO_NAME=${PRETTY_NAME:-Unknown}
FAMILY=unknown
for id in ${ID:-} ${ID_LIKE:-}; do
    case "$id" in
        debian|ubuntu|linuxmint|pop|elementary|zorin|kali|raspbian|neon) FAMILY=debian ;;
        fedora|rhel|centos|rocky|almalinux|nobara) FAMILY=fedora ;;
        arch|manjaro|endeavouros|garuda|artix|cachyos) FAMILY=arch ;;
        opensuse*|suse|sles) FAMILY=suse ;;
        void) FAMILY=void ;;
        alpine|postmarketos) FAMILY=alpine ;;
        gentoo|funtoo) FAMILY=gentoo ;;
    esac
    [[ $FAMILY != unknown ]] && break
done

if [[ -d /run/systemd/system ]]; then INIT=systemd
elif command -v openrc >/dev/null 2>&1 || [[ -d /etc/init.d && -x /sbin/openrc-run ]]; then INIT=openrc
elif [[ -d /etc/sv && -d /var/service ]] || command -v sv >/dev/null 2>&1; then INIT=runit
elif command -v systemctl >/dev/null 2>&1; then INIT=systemd-offline   # e.g. containers without a running systemd
else INIT=none
fi

step "System: $DISTRO_NAME (package family: $FAMILY, init: $INIT, kernel: $(uname -r))"

# Package names per distribution: pkg <role>
pkg() {
    case "$FAMILY:$1" in
        debian:base) echo "python3 python3-venv pciutils usbutils kmod" ;;
        debian:gui) echo "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" ;;
        debian:sensors) echo "lm-sensors" ;;
        debian:polkit) echo "pkexec" ;;
        debian:liquidctl) echo "liquidctl" ;;
        debian:appindicator) echo "gnome-shell-extension-appindicator" ;;
        debian:dkms) echo "dkms git build-essential linux-headers-$(uname -r)" ;;
        fedora:base) echo "python3 pciutils usbutils kmod" ;;
        fedora:gui) echo "python3-gobject gtk4 libadwaita" ;;
        fedora:sensors) echo "lm_sensors" ;;
        fedora:polkit) echo "polkit" ;;
        fedora:liquidctl) echo "liquidctl" ;;
        fedora:appindicator) echo "gnome-shell-extension-appindicator" ;;
        fedora:dkms) echo "dkms git make gcc kernel-devel-$(uname -r)" ;;
        arch:base) echo "python pciutils usbutils kmod" ;;
        arch:gui) echo "python-gobject gtk4 libadwaita" ;;
        arch:sensors) echo "lm_sensors" ;;
        arch:polkit) echo "polkit" ;;
        arch:liquidctl) echo "liquidctl" ;;
        arch:appindicator) echo "gnome-shell-extension-appindicator" ;;
        arch:dkms) echo "dkms git base-devel linux-headers" ;;
        suse:base) echo "python3 pciutils usbutils kmod" ;;
        suse:gui) echo "python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1" ;;
        suse:sensors) echo "sensors" ;;
        suse:polkit) echo "polkit" ;;
        suse:appindicator) echo "gnome-shell-extension-appindicator" ;;
        suse:dkms) echo "dkms git make gcc kernel-devel" ;;
        void:base) echo "python3 pciutils usbutils kmod" ;;
        void:gui) echo "python3-gobject gtk4 libadwaita" ;;
        void:sensors) echo "lm_sensors" ;;
        void:polkit) echo "polkit" ;;
        void:dkms) echo "dkms git base-devel linux-headers" ;;
        alpine:base) echo "python3 pciutils usbutils kmod bash" ;;
        alpine:gui) echo "py3-gobject3 gtk4.0 libadwaita" ;;
        alpine:sensors) echo "lm-sensors lm-sensors-detect" ;;
        alpine:polkit) echo "polkit" ;;
        alpine:liquidctl) echo "liquidctl" ;;
        gentoo:base) echo "dev-lang/python sys-apps/pciutils sys-apps/usbutils sys-apps/kmod" ;;
        gentoo:gui) echo "dev-python/pygobject gui-libs/gtk gui-libs/libadwaita" ;;
        gentoo:sensors) echo "sys-apps/lm-sensors" ;;
        gentoo:polkit) echo "sys-auth/polkit" ;;
        gentoo:liquidctl) echo "app-misc/liquidctl" ;;
        *) echo "" ;;
    esac
}

APT_UPDATED=0
pm_install() {   # all at once, one by one on failure (a missing package name does not abort)
    [[ $# -eq 0 ]] && return 0
    local rc=0
    case "$FAMILY" in
        debian)
            if [[ $APT_UPDATED -eq 0 ]]; then apt-get update >>"$LOG" 2>&1 || true; APT_UPDATED=1; fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" >>"$LOG" 2>&1 || rc=1 ;;
        fedora) dnf install -y "$@" >>"$LOG" 2>&1 || rc=1 ;;
        arch)
            # Fresh installations/containers have no package database yet.
            [[ -n "$(ls -A /var/lib/pacman/sync 2>/dev/null)" ]] || pacman -Sy --noconfirm >>"$LOG" 2>&1 || true
            pacman -S --needed --noconfirm "$@" >>"$LOG" 2>&1 || rc=1 ;;
        suse) zypper --non-interactive install --no-recommends "$@" >>"$LOG" 2>&1 || rc=1 ;;
        void) xbps-install -Sy "$@" >>"$LOG" 2>&1 || rc=1 ;;
        alpine)
            if [[ $APT_UPDATED -eq 0 ]]; then apk update >>"$LOG" 2>&1 || true; APT_UPDATED=1; fi
            apk add "$@" >>"$LOG" 2>&1 || rc=1 ;;
        gentoo) emerge --noreplace --quiet "$@" >>"$LOG" 2>&1 || rc=1 ;;
        *) return 1 ;;
    esac
    if [[ $rc -ne 0 && $# -gt 1 ]]; then
        rc=0
        for p in "$@"; do pm_install "$p" || { warn "Package could not be installed: $p"; rc=1; }; done
    fi
    return $rc
}

install_role() {   # install_role <role> <description>
    local names; names=$(pkg "$1")
    if [[ -z $names ]]; then
        warn "$2: no package names known for this distribution – please install manually"
        return 1
    fi
    # shellcheck disable=SC2086
    if pm_install $names; then ok "$2"; else warn "$2: not completely installed (details: $LOG)"; return 1; fi
}

have_python() { python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
have_gui() {
    python3 - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 14)
assert Adw.get_major_version() > 1 or Adw.get_minor_version() >= 5
EOF
}
have_liquidctl() {
    PYTHONPATH="$(ls -d "$LIBDIR"/venv/lib/python3*/site-packages 2>/dev/null | head -1)" \
        python3 -c 'import liquidctl' 2>/dev/null
}

# --- Dependencies --------------------------------------------------------------
if [[ $INSTALL_DEPS -eq 1 ]]; then
    step "Installing packages"
    if [[ -n $APPIMAGE_SRC ]]; then
        # The AppImage ships Python, GTK, libadwaita and liquidctl – only install system tools.
        # shellcheck disable=SC2046
        pm_install $(pkg base | tr ' ' '\n' | grep -v 'python' | xargs) && ok "System tools" || warn "System tools incomplete"
        install_role sensors "lm-sensors (sensors-detect)" || true
        install_role polkit "polkit (start the service from the UI)" || true
    else
    install_role base "Python and system tools" || true
    install_role sensors "lm-sensors (sensors-detect)" || true
    install_role polkit "polkit (start the service from the UI)" || true
    if have_gui; then ok "GTK 4 / libadwaita already present"; else install_role gui "User interface (GTK 4, libadwaita)" || true; fi
    if have_liquidctl; then ok "liquidctl already present"
    elif [[ -n $(pkg liquidctl) ]]; then install_role liquidctl "liquidctl (AIO liquid coolers, smart hubs)" || true
    else warn "liquidctl is not packaged here – it is installed via pip when AIO/smart devices are connected"
    fi
    fi
fi

if [[ -n $APPIMAGE_SRC ]]; then
    ok "Python, user interface and liquidctl come from the AppImage"
elif ! have_python; then
    fail "Python 3.10 or newer is required (found: $(python3 --version 2>&1 || echo none))."
    exit 1
fi
if have_gui; then
    ok "User interface: GTK 4.14+ and libadwaita 1.5+ present"
else
    hint "User interface cannot run: GTK ≥ 4.14 and libadwaita ≥ 1.5 are missing (e.g. Debian 12, Ubuntu 22.04) – use the AppImage. Service and fancontrol-linuxctl work anyway."
fi

# --- Program files ---------------------------------------------------------------
if [[ -n $APPIMAGE_SRC ]]; then
    step "Copying the AppImage contents to $OPT"
    rm -rf "${OPT:?}.new"
    cp -a "$APPIMAGE_SRC/." "$OPT.new"
    rm -rf "${OPT:?}" && mv "$OPT.new" "$OPT"
    rm -rf "${LIBDIR:?}"   # replace an earlier installation from source
    for mode in "fancontrol-linux:" "fancontrol-linuxd:--daemon" "fancontrol-linuxctl:--ctl"; do
        rm -f "$PREFIX/bin/${mode%%:*}"   # may be a symlink of the source installation
        printf '#!/bin/sh\nexec %s/AppRun %s "$@"\n' "$OPT" "${mode#*:}" > "$PREFIX/bin/${mode%%:*}"
        chmod 755 "$PREFIX/bin/${mode%%:*}"
    done
    install -Dm 644 "$OPT/usr/lib/fancontrol-linux/data/io.github.fancontrol_linux.desktop" "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
    install -Dm 644 "$OPT/usr/lib/fancontrol-linux/data/io.github.fancontrol_linux.svg" "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
    SRC="$OPT/usr/lib/fancontrol-linux"
    ln -sf "$SRC/upgrade.sh" "$PREFIX/bin/fancontrol-linux-upgrade"
    for pair in lifaco:fancontrol-linux lifacod:fancontrol-linuxd lifacoctl:fancontrol-linuxctl lifaco-upgrade:fancontrol-linux-upgrade; do
        ln -sf "$PREFIX/bin/${pair#*:}" "$PREFIX/bin/${pair%%:*}"   # short command names
    done
    ok "Program, commands, menu entry"
else
step "Copying the program to $LIBDIR"
VENV_BACKUP=""
if [[ -d "$LIBDIR/venv" ]]; then VENV_BACKUP=$(mktemp -d); mv "$LIBDIR/venv" "$VENV_BACKUP/"; fi
rm -rf "${LIBDIR:?}" "${OPT:?}"
install -d "$LIBDIR/bin"
cp -r "$SRC/fancontrol_linux" "$LIBDIR/"
find "$LIBDIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
[[ -n $VENV_BACKUP ]] && mv "$VENV_BACKUP/venv" "$LIBDIR/" && rmdir "$VENV_BACKUP"
for b in fancontrol-linux fancontrol-linuxd fancontrol-linuxctl; do
    install -m 755 "$SRC/bin/$b" "$LIBDIR/bin/$b"
    ln -sf "$LIBDIR/bin/$b" "$PREFIX/bin/$b"
done
install -m 755 "$SRC/upgrade.sh" "$LIBDIR/upgrade.sh"
ln -sf "$LIBDIR/upgrade.sh" "$PREFIX/bin/fancontrol-linux-upgrade"
for pair in lifaco:fancontrol-linux lifacod:fancontrol-linuxd lifacoctl:fancontrol-linuxctl lifaco-upgrade:fancontrol-linux-upgrade; do
    ln -sf "$PREFIX/bin/${pair#*:}" "$PREFIX/bin/${pair%%:*}"   # short command names
done
install -Dm 644 "$SRC/data/io.github.fancontrol_linux.desktop" "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
install -Dm 644 "$SRC/data/io.github.fancontrol_linux.svg" "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true
ok "Program, commands, menu entry"

# liquidctl not packaged? Then install it into a private Python environment (only if matching USB devices are present).
LIQUID_VENDORS="1e71|1b1c|3842|1044|0db0|0cf2|2433"
if [[ $INSTALL_DEPS -eq 1 ]] && ! have_liquidctl && lsusb 2>/dev/null | grep -iE "ID ($LIQUID_VENDORS):" >/dev/null; then
    if ask "USB cooling devices found but liquidctl is missing. Install it into a private Python environment (pip)?" y; then
        if python3 -m venv --system-site-packages "$LIBDIR/venv" >>"$LOG" 2>&1 \
            && "$LIBDIR/venv/bin/pip" install --quiet liquidctl >>"$LOG" 2>&1; then
            ok "liquidctl installed in $LIBDIR/venv"
        else
            hint "liquidctl could not be installed (details: $LOG)"
        fi
    fi
fi

fi

install -d -m 755 /etc/fancontrol-linux /etc/fancontrol-linux/profiles

step "User group 'fancontrol'"
getent group fancontrol >/dev/null || groupadd --system fancontrol 2>/dev/null || addgroup -S fancontrol
TARGET_USER="${SUDO_USER:-}"
RELOGIN=0
if [[ -n "$TARGET_USER" && "$TARGET_USER" != root ]]; then
    if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -x fancontrol >/dev/null; then
        usermod -aG fancontrol "$TARGET_USER" 2>/dev/null || addgroup "$TARGET_USER" fancontrol
        RELOGIN=1
    fi
    ok "$TARGET_USER is a member"
fi
# Directory for file sensors that group members may write to (persists across reboots).
install -d -m 2775 -g fancontrol "$STATE_DIR/sensors"
chmod 755 "$STATE_DIR"

# --- Hardware ---------------------------------------------------------------------
MODULES=()
load_module() {   # load_module <module>
    if modprobe "$1" >>"$LOG" 2>&1; then
        MODULES+=("$1")
        ok "Driver loaded: $1"
        return 0
    fi
    return 1
}
pwm_count() {   # no pipeline: a missing match must not abort under "set -e -o pipefail"
    local f n=0
    for f in /sys/class/hwmon/*/pwm[0-9]; do [[ -e $f ]] && n=$((n + 1)); done
    echo "$n"
}
dmi() { cat "/sys/class/dmi/id/$1" 2>/dev/null || true; }

regen_initramfs() {
    if command -v update-initramfs >/dev/null; then update-initramfs -u >>"$LOG" 2>&1
    elif command -v dracut >/dev/null; then dracut -f >>"$LOG" 2>&1
    elif command -v mkinitcpio >/dev/null; then mkinitcpio -P >>"$LOG" 2>&1
    else return 1
    fi
}

if [[ $SETUP_HARDWARE -eq 1 ]]; then
    step "Detecting mainboard sensors (sensors-detect)"
    BEFORE=$(pwm_count)
    if command -v sensors-detect >/dev/null; then
        DETECT_LOG=/var/log/fancontrol-linux-sensors-detect.log
        # --auto answers every question with the (safe) default answer.
        if yes "" | timeout 180 sensors-detect --auto > "$DETECT_LOG" 2>&1; then
            ok "Output saved to $DETECT_LOG"
        else
            warn "sensors-detect reported errors (details: $DETECT_LOG)"
        fi
        mapfile -t DETECTED < <(sed -n '/Chip drivers/,/#----cut here----/p' "$DETECT_LOG" \
            | grep -oE '^(modprobe )?[a-z0-9_-]+$' | sed 's/^modprobe //' | grep -vE '^(cut|chip)$' | sort -u || true)
        for m in "${DETECTED[@]}"; do load_module "$m" || warn "Driver $m could not be loaded"; done
        [[ ${#DETECTED[@]} -eq 0 ]] && warn "sensors-detect did not suggest any chip drivers"
        if grep -q "ITE" "$DETECT_LOG" && ! grep -qsx 'it87' /sys/class/hwmon/*/name; then
            ITE_UNSUPPORTED=1
        fi
    else
        warn "sensors-detect not found (package lm-sensors)"
    fi

    # Known conflicts with ACPI: it87/nct6775 are not loaded then.
    if dmesg 2>/dev/null | grep -iE "(it87|nct6775).*(resource conflict|ACPI)" >/dev/null; then
        CONFLICT=1
    fi
    if [[ ${ITE_UNSUPPORTED:-0} -eq 1 || ( ${CONFLICT:-0} -eq 1 && $(pwm_count) -eq 0 ) ]]; then
        warn "ITE chip found, but not supported or blocked by ACPI."
        if [[ $IT87_DKMS == yes ]] || ask "Install the current it87 driver (github.com/frankcrawford/it87) via DKMS?" n; then
            if install_role dkms "Build tools and kernel headers"; then
                rm -rf /usr/src/it87-fancontrol-linux
                if git clone --depth 1 https://github.com/frankcrawford/it87 /usr/src/it87-fancontrol-linux >>"$LOG" 2>&1 \
                    && (cd /usr/src/it87-fancontrol-linux && ./dkms-install.sh) >>"$LOG" 2>&1; then
                    ok "it87 installed via DKMS"
                    echo "options it87 ignore_resource_conflict=1" > /etc/modprobe.d/fancontrol-linux-it87.conf
                    record /etc/modprobe.d/fancontrol-linux-it87.conf
                    modprobe -r it87 >>"$LOG" 2>&1 || true
                    load_module it87 || hint "it87 does not load yet – check again after a reboot"
                else
                    hint "DKMS installation of it87 failed (details: $LOG)"
                fi
            fi
        else
            hint "For ITE chips: sudo ./install.sh --it87-dkms, or the kernel parameter acpi_enforce_resources=lax"
        fi
    fi

    step "Vendor-specific support"
    VENDOR=$(dmi sys_vendor)
    BOARD="$(dmi board_vendor) $(dmi board_name)"
    echo "  System: $VENDOR / $BOARD"
    case "${VENDOR,,}" in
        *dell*)
            load_module dell_smm_hwmon || { echo "options dell_smm_hwmon ignore_dmi=1" > /etc/modprobe.d/fancontrol-linux-dell.conf
                record /etc/modprobe.d/fancontrol-linux-dell.conf
                load_module dell_smm_hwmon || hint "Dell: dell_smm_hwmon does not support this model"; } ;;
        *lenovo*)
            if [[ -d /sys/module/thinkpad_acpi ]]; then
                if [[ $(cat /sys/module/thinkpad_acpi/parameters/fan_control 2>/dev/null) == Y ]]; then
                    ok "ThinkPad fan control is enabled"
                elif [[ $THINKPAD == yes ]] || ask "ThinkPad: enable fan control (thinkpad_acpi fan_control=1)?" y; then
                    echo "options thinkpad_acpi fan_control=1" > /etc/modprobe.d/fancontrol-linux-thinkpad.conf
                    record /etc/modprobe.d/fancontrol-linux-thinkpad.conf
                    hint "ThinkPad: fan control becomes active after a reboot"
                fi
            fi ;;
    esac
    if [[ "${VENDOR,,} ${BOARD,,}" == *asus* ]]; then
        load_module asus_ec_sensors || true
        load_module asus_wmi_sensors || true
    fi
    if [[ -d /sys/class/hwmon ]] && grep -qsE "^(nzxt|corsair|aquacomputer|d5next|octo|quadro|highflownext|kraken)" /sys/class/hwmon/*/name; then
        ok "USB cooling devices with kernel drivers: $(cat /sys/class/hwmon/*/name | grep -E '^(nzxt|corsair|aquacomputer|d5next|octo|quadro|highflownext|kraken)' | sort -u | tr '\n' ' ')"
    fi

    step "Graphics cards"
    GPUS=$(lspci -nn 2>/dev/null | grep -E '\[03[0-9a-f]{2}\]' || true)
    if grep -q '\[10de:' <<<"$GPUS"; then
        if [[ -d /sys/module/nvidia ]]; then
            if ldconfig -p 2>/dev/null | grep 'libnvidia-ml.so.1' >/dev/null; then
                ok "NVIDIA: driver and NVML present – fans controllable"
            else
                hint "NVIDIA: libnvidia-ml is missing (part of the NVIDIA driver package, e.g. libnvidia-compute-* / nvidia-utils)"
            fi
        elif [[ -d /sys/module/nouveau ]]; then
            hint "NVIDIA: the free nouveau driver can hardly control fans. Install the proprietary driver (Ubuntu: sudo ubuntu-drivers install)."
        else
            hint "NVIDIA: no driver loaded"
        fi
    fi
    if grep -q '\[1002:' <<<"$GPUS"; then
        for card in /sys/class/drm/card[0-9]; do
            [[ $(cat "$card/device/vendor" 2>/dev/null) == 0x1002 ]] || continue
            major=$(cat "$card/device/ip_discovery/die/0/GC/0/major" 2>/dev/null || echo 0)
            if [[ -e "$card/device/gpu_od/fan_ctrl/fan_curve" ]]; then
                ok "AMD $(basename "$card"): overdrive fan control active"
            elif [[ $major -ge 11 ]]; then
                warn "AMD $(basename "$card") (RDNA3/4): fan control needs overdrive enabled (amdgpu.ppfeaturemask)."
                if [[ $AMD_OVERDRIVE == yes ]] || ask "Enable overdrive? (kernel option, new initramfs, then reboot; the kernel then reports itself as 'tainted')" n; then
                    echo "options amdgpu ppfeaturemask=0xffffffff" > /etc/modprobe.d/fancontrol-linux-amdgpu.conf
                    record /etc/modprobe.d/fancontrol-linux-amdgpu.conf
                    if regen_initramfs; then hint "AMD: overdrive active after the next reboot"
                    else hint "AMD: the initramfs could not be regenerated – please do it manually (details: $LOG)"; fi
                else
                    hint "AMD RDNA3/4: sudo ./install.sh --amd-overdrive enables fan control"
                fi
            else
                ok "AMD $(basename "$card"): controlled through amdgpu"
            fi
        done
    fi
    if grep -qiE '\[8086:.*(arc|dg2|battlemage)' <<<"$GPUS"; then
        hint "Intel Arc: temperatures are shown, the Linux driver does not offer fan control yet"
    fi

    if [[ ${#MODULES[@]} -gt 0 ]]; then
        CONF=/etc/modules-load.d/fancontrol-linux.conf
        mkdir -p /etc/modules-load.d
        { echo "# Loaded by LiFaCo"; printf '%s\n' "${MODULES[@]}" | sort -u; } > "$CONF"
        record "$CONF"
        ok "Drivers are loaded automatically at boot ($CONF)"
    fi
    AFTER=$(pwm_count)
    echo "  Controllable mainboard/GPU outputs (hwmon): before $BEFORE, now $AFTER"
fi

# The tray under GNOME needs the AppIndicator extension.
if [[ $INSTALL_DEPS -eq 1 ]] && pgrep -x gnome-shell >/dev/null 2>&1; then
    step "GNOME: tray support"
    if install_role appindicator "AppIndicator extension"; then
        hint "GNOME: enable the extension 'AppIndicator and KStatusNotifierItem Support' in the Extensions app (after logging in again)"
    fi
fi

# --- Service --------------------------------------------------------------------------
if [[ $WITH_SERVICE -eq 1 ]]; then
    step "Background service ($INIT)"
    case "$INIT" in
        systemd|systemd-offline)
            install -Dm 644 "$SRC/data/fancontrol-linux.service" /etc/systemd/system/fancontrol-linux.service
            if [[ $INIT == systemd ]]; then
                systemctl daemon-reload
                if systemctl is-active --quiet fancontrol.service; then
                    warn "Disabling the lm-sensors service 'fancontrol' (both would interfere)"
                    systemctl disable --now fancontrol.service || true
                fi
                systemctl enable fancontrol-linux.service >>"$LOG" 2>&1
                systemctl restart fancontrol-linux.service
                sleep 1
                if systemctl is-active --quiet fancontrol-linux.service; then ok "Service running"
                else fail "Service does not start – journalctl -u fancontrol-linux shows the cause"; fi
            else
                systemctl enable fancontrol-linux.service >>"$LOG" 2>&1 || true
                warn "systemd is not running here (container?) – the service is set up and starts at the next boot"
            fi ;;
        openrc)
            install -Dm 755 "$SRC/data/openrc/fancontrol-linux" /etc/init.d/fancontrol-linux
            rc-update add fancontrol-linux default >>"$LOG" 2>&1 || true
            rc-service fancontrol-linux restart >>"$LOG" 2>&1 && ok "Service running" || warn "Service could not be started (details: $LOG)" ;;
        runit)
            install -Dm 755 "$SRC/data/runit/run" /etc/sv/fancontrol-linux/run
            ln -sfn /etc/sv/fancontrol-linux /var/service/fancontrol-linux 2>/dev/null \
                || ln -sfn /etc/sv/fancontrol-linux /etc/runit/runsvdir/default/fancontrol-linux 2>/dev/null || true
            ok "runit service set up" ;;
        *)
            hint "No supported init system found – start the service manually: sudo fancontrol-linuxd" ;;
    esac
fi

# --- Summary -------------------------------------------------------------------
step "Detected hardware"
if [[ -n $APPIMAGE_SRC ]]; then SUMMARY=("$OPT/AppRun" --python -); else SUMMARY=(env PYTHONPATH="$LIBDIR" python3 -); fi
"${SUMMARY[@]}" <<'EOF' 2>>"$LOG" || warn "Overview not available (details: $LOG)"
from fancontrol_linux.hwmon import Hardware
marks = {"ok": "✓", "warn": "!", "off": "-"}
for row in Hardware().info():
    print(f"  {marks.get(row['state'], '?')} {row['name']}: {row['detail']}")
EOF

echo
if [[ ${#HINTS[@]} -gt 0 ]]; then
    echo "${B}Notes:${N}"
    printf '  • %s\n' "${HINTS[@]}"
    echo
fi
echo "${G}Done.${N} Start 'LiFaCo' from the application menu or with: lifaco"
echo "Log: $LOG"
echo "Update later with: sudo lifaco-upgrade"
if [[ $RELOGIN -eq 1 ]]; then
    echo "${B}IMPORTANT:${N} '$TARGET_USER' was added to the group 'fancontrol' – please log out and back in once."
fi
