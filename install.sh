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

  -y, --yes            No questions; safe default answers (installs missing drivers and libraries,
                       but makes no boot changes such as kernel parameters or graphics drivers)
  --no-deps            Do not install packages
  --no-hardware        Do not detect/load drivers (sensors-detect, vendor modules)
  --no-service         Do not set up the background service
  --amd-overdrive      AMD RDNA3/4: enable overdrive for fan control without asking
  --it87-dkms          Install the newer it87 driver (ITE chips) via DKMS without asking
                       (done automatically when an unsupported ITE chip is found)
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

has_ite_hwmon() { grep -sE '^it8' /sys/class/hwmon/*/name >/dev/null; }
secure_boot() { [[ $(mokutil --sb-state 2>/dev/null || true) == *"SecureBoot enabled"* ]]; }
REBOOT=0

# Boot-time changes (kernel parameters, graphics drivers) are only made when someone answers at the terminal,
# never with --yes.
ask_boot() { [[ $ASSUME_YES -eq 0 && -t 0 ]] && ask "$1" y; }

# Adds a kernel parameter through the boot loader; uninstall.sh removes it again.
add_kernel_param() {
    local param=$1 line cfg
    if command -v grubby >/dev/null; then   # Fedora/RHEL (BLS entries)
        grubby --update-kernel=ALL --args="$param" >>"$LOG" 2>&1 || return 1
    elif [[ -f /etc/default/grub ]]; then   # Debian/Ubuntu, Arch, openSUSE …
        [[ -f /etc/default/grub.fancontrol-linux.bak ]] || cp /etc/default/grub /etc/default/grub.fancontrol-linux.bak
        line=$(grep -E '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub || true)
        if [[ -z $line ]]; then
            echo "GRUB_CMDLINE_LINUX_DEFAULT=\"$param\"" >> /etc/default/grub
        elif ! [[ $line =~ [\"\ ]$param([\"\ ]|$) ]]; then
            sed -i -E -e "s/^(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*)\"/\1 $param\"/" -e "s/^(GRUB_CMDLINE_LINUX_DEFAULT=\") +/\1/" /etc/default/grub
        fi
        if command -v update-grub >/dev/null; then update-grub >>"$LOG" 2>&1 || return 1
        else
            cfg=/boot/grub/grub.cfg; [[ -d /boot/grub2 ]] && cfg=/boot/grub2/grub.cfg
            if command -v grub2-mkconfig >/dev/null; then grub2-mkconfig -o "$cfg" >>"$LOG" 2>&1 || return 1
            elif command -v grub-mkconfig >/dev/null; then grub-mkconfig -o "$cfg" >>"$LOG" 2>&1 || return 1
            else return 1
            fi
        fi
    elif command -v kernelstub >/dev/null; then   # Pop!_OS (systemd-boot)
        kernelstub -a "$param" >>"$LOG" 2>&1 || return 1
    else
        return 1
    fi
    mkdir -p "$STATE_DIR"
    grep -qxF "$param" "$STATE_DIR/kernel-params" 2>/dev/null || echo "$param" >> "$STATE_DIR/kernel-params"
    REBOOT=1
}

# Loads it87 and checks that the ITE chip really shows up. ignore_resource_conflict is needed on many
# Gigabyte/BIOSTAR/ASRock boards, where ACPI claims the chip's I/O range.
load_it87() {
    local conf=/etc/modprobe.d/fancontrol-linux-it87.conf
    echo "options it87 ignore_resource_conflict=1" > "$conf"
    record "$conf"
    modprobe -r it87 >>"$LOG" 2>&1 || true
    if modprobe it87 >>"$LOG" 2>&1 && has_ite_hwmon; then
        MODULES+=(it87)
        ok "Driver loaded: it87${ITE_CHIP:+ ($ITE_CHIP)}"
        return 0
    fi
    return 1
}

it87_dkms_installed() { [[ $(dkms status -m it87 -k "$(uname -r)" 2>/dev/null || true) == *installed* ]]; }

# The it87 driver from github.com/frankcrawford/it87 supports many newer ITE chips (IT8613E, IT8686E,
# IT8688E, IT8689E …) that the kernel's own driver does not know yet. DKMS rebuilds it for every new kernel.
install_it87_dkms() {
    local build
    if it87_dkms_installed; then ok "it87 (DKMS) is already installed"; return 0; fi
    install_role dkms "Build tools and kernel headers" || return 1
    build=$(mktemp -d)
    if ! git clone --depth 1 https://github.com/frankcrawford/it87 "$build/it87" >>"$LOG" 2>&1; then
        rm -rf "$build"
        hint "it87: download from GitHub failed (details: $LOG)"
        return 1
    fi
    # Built from a directory named "it87": the DKMS package name has to match PACKAGE_NAME in its dkms.conf.
    # The Makefile's final modprobe may fail (e.g. Secure Boot) – that is checked separately.
    (cd "$build/it87" && make dkms TARGET="$(uname -r)") >>"$LOG" 2>&1 || true
    rm -rf "$build"
    if it87_dkms_installed; then
        ok "it87 installed via DKMS (rebuilt automatically for new kernels)"
        return 0
    fi
    hint "it87: building the driver failed (details: $LOG)"
    return 1
}

# With Secure Boot, self-built modules only load once their signing key is enrolled (MOK).
enroll_mok() {
    local key=/var/lib/shim-signed/mok/MOK.der
    [[ -f $key ]] || key=/var/lib/dkms/mok.pub
    if [[ ! -f $key ]] || ! command -v mokutil >/dev/null; then
        hint "Secure Boot blocks the self-built fan driver – disable Secure Boot or sign the it87 module"
        return
    fi
    if [[ $(mokutil --test-key "$key" 2>&1 || true) == *"already enrolled"* ]]; then
        hint "it87 does not load yet – reboot once (details: sudo dmesg | grep it87)"
        REBOOT=1
        return
    fi
    warn "Secure Boot is on: the fan driver is signed with a key the firmware does not know yet."
    if [[ $ASSUME_YES -eq 0 && -t 0 ]] && ask "Register the key now? You choose a one-time password and confirm it once after the reboot" y; then
        if mokutil --import "$key"; then
            hint "Secure Boot: after the reboot choose 'Enroll MOK' → 'Continue' → 'Yes' and enter the password – then the fan driver loads"
            REBOOT=1
            return
        fi
    fi
    hint "Secure Boot: register the key with 'sudo mokutil --import $key', reboot and choose 'Enroll MOK'"
}

have_nvml() {
    ldconfig -p 2>/dev/null | grep 'libnvidia-ml.so.1' >/dev/null && return 0
    compgen -G "/usr/lib*/libnvidia-ml.so.1" >/dev/null || compgen -G "/usr/lib/*-linux-gnu/libnvidia-ml.so.1" >/dev/null
}
# Version of the NVIDIA driver: the loaded kernel module, otherwise the one installed for the running kernel.
nv_driver_version() {
    cat /sys/module/nvidia/version 2>/dev/null || modinfo -F version nvidia 2>/dev/null || true
}
nvml_version() {   # version of the installed libnvidia-ml, e.g. 595.91.07
    local lib
    lib=$(ldconfig -p 2>/dev/null | awk '/libnvidia-ml\.so\.1 / { path = $NF } END { print path }')
    [[ -n $lib ]] || return 0
    lib=$(readlink -f "$lib")
    [[ $lib == *.so.[0-9]* ]] && echo "${lib##*.so.}"
}
# The driver was installed with NVIDIA's .run installer: NVML comes from it, distribution packages would collide.
nv_runfile() { [[ -x /usr/bin/nvidia-uninstall ]]; }
apt_exact() {   # apt_exact <package> <driver version> → "package=<version>" for the build of exactly this driver
    local ver
    ver=$(apt-cache madison "$1" 2>/dev/null | awk -v v="$2" '!found && $3 ~ ("^" v "-") { found = $3 } END { print found }')
    if [[ -n $ver ]]; then echo "$1=$ver"; else echo "$1"; fi
}
# Packages that provide NVML and nvidia-smi for the loaded driver – works for every driver branch/version.
nvml_packages() {
    local version major suffix="" p
    version=$(nv_driver_version); major=${version%%.*}
    case "$FAMILY" in
        debian)
            # Ubuntu/Mint/Pop: libnvidia-compute-<branch>[-server] + nvidia-utils-<branch>[-server], pinned to the
            # exact version of the loaded module; Debian: libnvidia-ml1 + nvidia-smi (versioned with nvidia-driver).
            if [[ -n $major ]] && [[ $(dpkg-query -W -f='${Package} ${Status}\n' "*nvidia*-$major-server" 2>/dev/null || true) == *"install ok installed"* ]]; then
                suffix="-server"
            fi
            if [[ -n $major ]] && apt-cache show "libnvidia-compute-$major$suffix" >/dev/null 2>&1; then
                for p in "libnvidia-compute-$major$suffix" "nvidia-utils-$major$suffix"; do
                    apt-cache show "$p" >/dev/null 2>&1 && apt_exact "$p" "$version"
                done
            else
                for p in libnvidia-ml1 nvidia-smi; do apt_exact "$p" "$version"; done
            fi ;;
        fedora) echo "xorg-x11-drv-nvidia-cuda-libs xorg-x11-drv-nvidia-cuda" ;;   # RPM Fusion, same version as the driver
        arch) echo "nvidia-utils" ;;   # legacy branches (nvidia-470xx-utils …) come from the AUR
        suse)
            if [[ -n $major && $major -lt 500 ]]; then echo "nvidia-compute-G05 nvidia-compute-utils-G05"
            else echo "nvidia-compute-G06 nvidia-compute-utils-G06"; fi ;;
    esac
}
nvml_matches() { [[ -z $(nvml_version) || $(nvml_version) == "$(nv_driver_version)" ]]; }

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
    else
        warn "sensors-detect not found (package lm-sensors)"
    fi

    # Super-I/O chip as reported by sensors-detect, e.g. "IT8613E". "to-be-written" or an unknown ID means
    # the chip is too new for the kernel's own driver.
    ITE_CHIP=""
    if [[ -n ${DETECT_LOG:-} && -f $DETECT_LOG ]]; then
        ITE_CHIP=$(awk '/Trying family .ITE.*Yes/ { getline
            if (match($0, /IT ?8[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][A-Z]?/)) { s = substr($0, RSTART, RLENGTH); gsub(/ /, "", s); print s; exit }
            if (match($0, /ID 0x8[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]/)) { print "IT" substr($0, RSTART + 5, 4); exit } }' "$DETECT_LOG")
    fi
    BOARD_VENDOR=$(dmi board_vendor)

    step "Checking for known problems"
    NEED_IT87=0
    if [[ -n $ITE_CHIP ]] && ! has_ite_hwmon; then
        if ! load_it87; then
            NEED_IT87=1
            warn "Mainboard chip ITE $ITE_CHIP: the kernel's it87 driver ($(uname -r)) does not support it yet."
        fi
    elif [[ -z $ITE_CHIP && $(pwm_count) -eq 0 && ${BOARD_VENDOR,,} =~ (biostar|gigabyte|asrock) ]]; then
        if ! load_it87; then
            NEED_IT87=1
            warn "No fan controller found. $BOARD_VENDOR boards usually have ITE chips that need a newer it87 driver."
        fi
    fi
    if [[ $NEED_IT87 -eq 1 ]]; then
        if [[ $IT87_DKMS == yes ]] || ask "Install the newer it87 driver (github.com/frankcrawford/it87, via DKMS)? Recommended" y; then
            if install_it87_dkms; then
                if load_it87; then ok "Mainboard fans are now available"
                elif secure_boot; then enroll_mok
                else
                    hint "it87 is installed but does not load yet – reboot once (details: sudo dmesg | grep it87)"
                    REBOOT=1
                fi
            fi
        else
            hint "ITE chip${ITE_CHIP:+ $ITE_CHIP}: 'sudo ./install.sh --it87-dkms' installs the newer driver"
        fi
    elif [[ -n $ITE_CHIP ]]; then
        ok "Mainboard chip ITE $ITE_CHIP is supported"
    fi

    # ACPI claims the I/O range of the sensor chip (common with Nuvoton chips on MSI/ASRock boards).
    if [[ $NEED_IT87 -eq 0 && $(pwm_count) -eq 0 ]] && ! grep -w acpi_enforce_resources=lax /proc/cmdline >/dev/null \
        && dmesg 2>/dev/null | grep -iE "(it87|nct6775|nct6683|w83627|f71882).*(resource conflict|ACPI)" >/dev/null; then
        warn "The mainboard sensor chip is blocked by an ACPI resource conflict."
        if ask_boot "Add the kernel parameter acpi_enforce_resources=lax (the usual fix, active after a reboot)?"; then
            if add_kernel_param acpi_enforce_resources=lax; then
                hint "Kernel parameter acpi_enforce_resources=lax added – the mainboard fans appear after a reboot"
            else
                hint "The kernel parameter could not be added automatically – add acpi_enforce_resources=lax in your boot loader"
            fi
        else
            hint "ACPI conflict: the kernel parameter acpi_enforce_resources=lax usually helps (run sudo ./install.sh interactively to add it)"
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
                    REBOOT=1
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
        if [[ -d /sys/module/nvidia ]] || modinfo nvidia >/dev/null 2>&1; then
            NV_VERSION=$(nv_driver_version)
            if [[ -d /sys/module/nvidia ]]; then
                echo "  NVIDIA driver: ${NV_VERSION:-unknown version} (loaded)"
            else
                echo "  NVIDIA driver: ${NV_VERSION:-unknown version} (installed, not loaded yet)"
                hint "NVIDIA: driver $NV_VERSION is installed but not loaded – reboot once"
                REBOOT=1
            fi
            if ! have_nvml || ! nvml_matches; then
                if have_nvml; then
                    warn "NVIDIA: NVML $(nvml_version) does not match the loaded driver $NV_VERSION"
                else
                    warn "NVIDIA: the driver is loaded, but NVML (libnvidia-ml) is missing"
                fi
                if nv_runfile; then
                    warn "NVIDIA: the driver was installed with NVIDIA's .run installer – no distribution packages are added"
                elif [[ $INSTALL_DEPS -eq 1 && -n $(nvml_packages) ]]; then
                    echo "  Installing matching packages: $(nvml_packages | xargs)"
                    # shellcheck disable=SC2046
                    pm_install $(nvml_packages) || true
                fi
            fi
            if ! have_nvml; then
                if nv_runfile; then
                    hint "NVIDIA: NVML is missing – run NVIDIA's .run installer of version $NV_VERSION again"
                else
                    hint "NVIDIA: install NVML/nvidia-utils of driver $NV_VERSION from your distribution (package names: $(nvml_packages | xargs))"
                fi
            elif ! nvml_matches; then
                hint "NVIDIA: driver $NV_VERSION is loaded, but NVML $(nvml_version) is installed – reboot so both match (otherwise NVML reports a version mismatch)"
                REBOOT=1
            else
                ok "NVIDIA: driver $NV_VERSION and NVML $(nvml_version) match – fans controllable"
            fi
        elif command -v ubuntu-drivers >/dev/null && [[ $INSTALL_DEPS -eq 1 ]]; then
            warn "NVIDIA: the proprietary driver is not in use – only it can control the fans"
            if ask_boot "Install the recommended NVIDIA driver now (ubuntu-drivers install, active after a reboot)?"; then
                if ubuntu-drivers install >>"$LOG" 2>&1; then
                    hint "NVIDIA: driver installed – the graphics card fans can be controlled after a reboot"
                    REBOOT=1
                else
                    hint "NVIDIA: driver installation failed (details: $LOG)"
                fi
            else
                hint "NVIDIA: install the proprietary driver for fan control (sudo ubuntu-drivers install)"
            fi
        elif [[ -d /sys/module/nouveau ]]; then
            hint "NVIDIA: the free nouveau driver can hardly control fans – install the proprietary NVIDIA driver of your distribution."
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
                    if regen_initramfs; then hint "AMD: overdrive active after the next reboot"; REBOOT=1
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
    echo "  Controllable fan outputs via hwmon (mainboard, AMD graphics): before $BEFORE, now $AFTER"
    if grep -q '\[10de:' <<<"$GPUS"; then echo "  (NVIDIA fans are controlled through NVML and are not counted here)"; fi
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
if [[ $REBOOT -eq 1 ]]; then
    echo "${B}IMPORTANT:${N} please reboot once so the new drivers/settings become active."
fi
if [[ $RELOGIN -eq 1 ]]; then
    echo "${B}IMPORTANT:${N} '$TARGET_USER' was added to the group 'fancontrol' – please log out and back in once."
fi
