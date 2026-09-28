#!/usr/bin/env bash
# FanControl for Linux – Installer für Debian/Ubuntu/Mint, Fedora/RHEL, Arch/Manjaro, openSUSE, Void, Alpine, Gentoo.
# Installiert Programm, Abhängigkeiten, Hardware-Treiber und den Hintergrunddienst (systemd, OpenRC oder runit).
set -euo pipefail

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
Aufruf: sudo ./install.sh [Optionen]

  -y, --yes            Keine Rückfragen; sichere Standardantworten (keine Kernel-/Boot-Änderungen)
  --no-deps            Keine Pakete installieren
  --no-hardware        Keine Treiber erkennen/laden (sensors-detect, Hersteller-Module)
  --no-service         Hintergrunddienst nicht einrichten
  --amd-overdrive      AMD RDNA3/4: Overdrive für die Lüftersteuerung ohne Rückfrage aktivieren
  --it87-dkms          Aktuellen it87-Treiber (ITE-Chips) per DKMS ohne Rückfrage installieren
  --thinkpad-fan       ThinkPad: Lüftersteuerung (thinkpad_acpi fan_control=1) ohne Rückfrage freischalten
  -h, --help           Diese Hilfe

Aus dem AppImage: sudo ./FanControl-x86_64.AppImage --install [Optionen]
EOF
}

APPIMAGE_SRC=""
OPT=/opt/fancontrol-linux
while [[ $# -gt 0 ]]; do
    arg=$1
    shift
    case "$arg" in
        --appimage) APPIMAGE_SRC=${1:?--appimage braucht ein Verzeichnis}; shift ;;
        -y|--yes) ASSUME_YES=1 ;;
        --no-deps) INSTALL_DEPS=0 ;;
        --no-hardware) SETUP_HARDWARE=0 ;;
        --no-service) WITH_SERVICE=0 ;;
        --amd-overdrive) AMD_OVERDRIVE=yes ;;
        --it87-dkms) IT87_DKMS=yes ;;
        --thinkpad-fan) THINKPAD=yes ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unbekannte Option: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausführen: sudo ./install.sh" >&2
    exit 1
fi

# --- Ausgabe ------------------------------------------------------------------
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
step() { echo; echo "${B}==> $*${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
fail() { echo "  ${R}✗${N} $*"; }
HINTS=()
hint() { HINTS+=("$*"); warn "$*"; }

# ask "Frage" default(y/n) → 0 = ja
ask() {
    local question=$1 default=$2 answer
    if [[ $ASSUME_YES -eq 1 || ! -t 0 ]]; then
        [[ $default == y ]]
        return
    fi
    local suffix="[j/N]"; [[ $default == y ]] && suffix="[J/n]"
    read -r -p "  ? $question $suffix " answer || answer=
    answer=${answer,,}
    [[ -z $answer ]] && answer=$default
    [[ $answer == j* || $answer == y* ]]
}

record() { mkdir -p "$STATE_DIR"; grep -qxF "$1" "$MANIFEST" 2>/dev/null || echo "$1" >> "$MANIFEST"; }
: > "$LOG"

# --- Distribution erkennen ------------------------------------------------------
. /etc/os-release 2>/dev/null || true
DISTRO_NAME=${PRETTY_NAME:-Unbekannt}
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
elif command -v systemctl >/dev/null 2>&1; then INIT=systemd-offline   # z. B. Container ohne laufendes systemd
else INIT=none
fi

step "System: $DISTRO_NAME (Paketfamilie: $FAMILY, Init: $INIT, Kernel: $(uname -r))"

# Paketnamen je Distribution: pkg <rolle>
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
pm_install() {   # alle auf einmal, bei Fehler einzeln (ein fehlender Paketname bricht nichts ab)
    [[ $# -eq 0 ]] && return 0
    local rc=0
    case "$FAMILY" in
        debian)
            if [[ $APT_UPDATED -eq 0 ]]; then apt-get update >>"$LOG" 2>&1 || true; APT_UPDATED=1; fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" >>"$LOG" 2>&1 || rc=1 ;;
        fedora) dnf install -y "$@" >>"$LOG" 2>&1 || rc=1 ;;
        arch)
            # Frische Installationen/Container haben noch keine Paketdatenbank.
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
        for p in "$@"; do pm_install "$p" || { warn "Paket nicht installierbar: $p"; rc=1; }; done
    fi
    return $rc
}

install_role() {   # install_role <rolle> <beschreibung>
    local names; names=$(pkg "$1")
    if [[ -z $names ]]; then
        warn "$2: für diese Distribution keine Paketnamen bekannt – bitte manuell installieren"
        return 1
    fi
    # shellcheck disable=SC2086
    if pm_install $names; then ok "$2"; else warn "$2: nicht vollständig installiert (Details: $LOG)"; return 1; fi
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

# --- Abhängigkeiten -------------------------------------------------------------
if [[ $INSTALL_DEPS -eq 1 ]]; then
    step "Pakete installieren"
    if [[ -n $APPIMAGE_SRC ]]; then
        # Python, GTK, libadwaita und liquidctl bringt das AppImage mit – nur Systemwerkzeuge installieren.
        # shellcheck disable=SC2046
        pm_install $(pkg base | tr ' ' '\n' | grep -v 'python' | xargs) && ok "Systemwerkzeuge" || warn "Systemwerkzeuge unvollständig"
        install_role sensors "lm-sensors (sensors-detect)" || true
        install_role polkit "polkit (Dienst aus der Oberfläche starten)" || true
    else
    install_role base "Python und Systemwerkzeuge" || true
    install_role sensors "lm-sensors (sensors-detect)" || true
    install_role polkit "polkit (Dienst aus der Oberfläche starten)" || true
    if have_gui; then ok "GTK 4 / libadwaita bereits vorhanden"; else install_role gui "Oberfläche (GTK 4, libadwaita)" || true; fi
    if have_liquidctl; then ok "liquidctl bereits vorhanden"
    elif [[ -n $(pkg liquidctl) ]]; then install_role liquidctl "liquidctl (AIO-Wasserkühlungen, Smart-Hubs)" || true
    else warn "liquidctl gibt es hier nicht als Paket – wird bei angeschlossenen AIO/Smart-Geräten per pip nachinstalliert"
    fi
    fi
fi

if [[ -n $APPIMAGE_SRC ]]; then
    ok "Python, Oberfläche und liquidctl kommen aus dem AppImage"
elif ! have_python; then
    fail "Python 3.10 oder neuer wird benötigt (gefunden: $(python3 --version 2>&1 || echo keins))."
    exit 1
fi
if have_gui; then
    ok "Oberfläche: GTK 4.14+ und libadwaita 1.5+ vorhanden"
else
    hint "Oberfläche nicht lauffähig: GTK ≥ 4.14 und libadwaita ≥ 1.5 fehlen (z. B. Debian 12, Ubuntu 22.04). Dienst und fancontrol-linuxctl funktionieren trotzdem."
fi

# --- Programmdateien --------------------------------------------------------------
if [[ -n $APPIMAGE_SRC ]]; then
    step "AppImage-Inhalt nach $OPT kopieren"
    rm -rf "${OPT:?}.new"
    cp -a "$APPIMAGE_SRC/." "$OPT.new"
    rm -rf "${OPT:?}" && mv "$OPT.new" "$OPT"
    rm -rf "${LIBDIR:?}"   # eine frühere Installation aus dem Quellcode ersetzen
    for mode in "fancontrol-linux:" "fancontrol-linuxd:--daemon" "fancontrol-linuxctl:--ctl"; do
        rm -f "$PREFIX/bin/${mode%%:*}"   # kann ein Symlink der Quellcode-Installation sein
        printf '#!/bin/sh\nexec %s/AppRun %s "$@"\n' "$OPT" "${mode#*:}" > "$PREFIX/bin/${mode%%:*}"
        chmod 755 "$PREFIX/bin/${mode%%:*}"
    done
    install -Dm 644 "$OPT/usr/lib/fancontrol-linux/data/io.github.fancontrol_linux.desktop" "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
    install -Dm 644 "$OPT/usr/lib/fancontrol-linux/data/io.github.fancontrol_linux.svg" "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
    SRC="$OPT/usr/lib/fancontrol-linux"
    ok "Programm, Befehle, Startmenü-Eintrag"
else
step "Programm nach $LIBDIR kopieren"
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
install -Dm 644 "$SRC/data/io.github.fancontrol_linux.desktop" "$PREFIX/share/applications/io.github.fancontrol_linux.desktop"
install -Dm 644 "$SRC/data/io.github.fancontrol_linux.svg" "$PREFIX/share/icons/hicolor/scalable/apps/io.github.fancontrol_linux.svg"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true
ok "Programm, Befehle, Startmenü-Eintrag"

# liquidctl nicht als Paket verfügbar? Dann in eine private Python-Umgebung (nur wenn passende USB-Geräte da sind).
LIQUID_VENDORS="1e71|1b1c|3842|1044|0db0|0cf2|2433"
if [[ $INSTALL_DEPS -eq 1 ]] && ! have_liquidctl && lsusb 2>/dev/null | grep -qiE "ID ($LIQUID_VENDORS):"; then
    if ask "USB-Kühlgeräte gefunden, liquidctl fehlt. In eine private Python-Umgebung installieren (pip)?" y; then
        if python3 -m venv --system-site-packages "$LIBDIR/venv" >>"$LOG" 2>&1 \
            && "$LIBDIR/venv/bin/pip" install --quiet liquidctl >>"$LOG" 2>&1; then
            ok "liquidctl in $LIBDIR/venv installiert"
        else
            hint "liquidctl konnte nicht installiert werden (Details: $LOG)"
        fi
    fi
fi

fi

install -d -m 755 /etc/fancontrol-linux /etc/fancontrol-linux/profiles

step "Benutzergruppe 'fancontrol'"
getent group fancontrol >/dev/null || groupadd --system fancontrol 2>/dev/null || addgroup -S fancontrol
TARGET_USER="${SUDO_USER:-}"
RELOGIN=0
if [[ -n "$TARGET_USER" && "$TARGET_USER" != root ]]; then
    if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx fancontrol; then
        usermod -aG fancontrol "$TARGET_USER" 2>/dev/null || addgroup "$TARGET_USER" fancontrol
        RELOGIN=1
    fi
    ok "$TARGET_USER ist Mitglied"
fi
# Ordner für Datei-Sensoren, den Mitglieder der Gruppe beschreiben dürfen (bleibt über Neustarts erhalten).
install -d -m 2775 -g fancontrol "$STATE_DIR/sensors"
chmod 755 "$STATE_DIR"

# --- Hardware ---------------------------------------------------------------------
MODULES=()
load_module() {   # load_module <modul> [persistent]
    if modprobe "$1" >>"$LOG" 2>&1; then
        MODULES+=("$1")
        ok "Treiber geladen: $1"
        return 0
    fi
    return 1
}
pwm_count() {   # ohne Pipeline: ein fehlender Treffer darf unter "set -e -o pipefail" nicht abbrechen
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
    step "Mainboard-Sensoren erkennen (sensors-detect)"
    BEFORE=$(pwm_count)
    if command -v sensors-detect >/dev/null; then
        DETECT_LOG=/var/log/fancontrol-linux-sensors-detect.log
        # --auto beantwortet alle Fragen mit der (sicheren) Standardantwort.
        if yes "" | timeout 180 sensors-detect --auto > "$DETECT_LOG" 2>&1; then
            ok "Ausgabe gespeichert in $DETECT_LOG"
        else
            warn "sensors-detect meldete Fehler (Details: $DETECT_LOG)"
        fi
        mapfile -t DETECTED < <(sed -n '/Chip drivers/,/#----cut here----/p' "$DETECT_LOG" \
            | grep -oE '^(modprobe )?[a-z0-9_-]+$' | sed 's/^modprobe //' | grep -vE '^(cut|chip)$' | sort -u || true)
        for m in "${DETECTED[@]}"; do load_module "$m" || warn "Treiber $m ließ sich nicht laden"; done
        [[ ${#DETECTED[@]} -eq 0 ]] && warn "sensors-detect hat keine Chip-Treiber vorgeschlagen"
        if grep -q "ITE" "$DETECT_LOG" && ! grep -qsx 'it87' /sys/class/hwmon/*/name; then
            ITE_UNSUPPORTED=1
        fi
    else
        warn "sensors-detect nicht gefunden (Paket lm-sensors)"
    fi

    # Bekannte Konflikte mit ACPI: it87/nct6775 werden dann nicht geladen.
    if dmesg 2>/dev/null | grep -qiE "(it87|nct6775).*(resource conflict|ACPI)"; then
        CONFLICT=1
    fi
    if [[ ${ITE_UNSUPPORTED:-0} -eq 1 || ( ${CONFLICT:-0} -eq 1 && $(pwm_count) -eq 0 ) ]]; then
        warn "ITE-Chip gefunden, aber nicht unterstützt oder durch ACPI blockiert."
        if [[ $IT87_DKMS == yes ]] || ask "Aktuellen it87-Treiber (github.com/frankcrawford/it87) per DKMS installieren?" n; then
            if install_role dkms "Build-Werkzeuge und Kernel-Header"; then
                rm -rf /usr/src/it87-fancontrol-linux
                if git clone --depth 1 https://github.com/frankcrawford/it87 /usr/src/it87-fancontrol-linux >>"$LOG" 2>&1 \
                    && (cd /usr/src/it87-fancontrol-linux && ./dkms-install.sh) >>"$LOG" 2>&1; then
                    ok "it87 per DKMS installiert"
                    echo "options it87 ignore_resource_conflict=1" > /etc/modprobe.d/fancontrol-linux-it87.conf
                    record /etc/modprobe.d/fancontrol-linux-it87.conf
                    modprobe -r it87 >>"$LOG" 2>&1 || true
                    load_module it87 || hint "it87 lädt noch nicht – nach einem Neustart erneut prüfen"
                else
                    hint "DKMS-Installation von it87 fehlgeschlagen (Details: $LOG)"
                fi
            fi
        else
            hint "Für ITE-Chips: sudo ./install.sh --it87-dkms, oder Kernelparameter acpi_enforce_resources=lax"
        fi
    fi

    step "Hersteller-spezifische Unterstützung"
    VENDOR=$(dmi sys_vendor)
    BOARD="$(dmi board_vendor) $(dmi board_name)"
    echo "  System: $VENDOR / $BOARD"
    case "${VENDOR,,}" in
        *dell*)
            load_module dell_smm_hwmon || { echo "options dell_smm_hwmon ignore_dmi=1" > /etc/modprobe.d/fancontrol-linux-dell.conf
                record /etc/modprobe.d/fancontrol-linux-dell.conf
                load_module dell_smm_hwmon || hint "Dell: dell_smm_hwmon wird von diesem Modell nicht unterstützt"; } ;;
        *lenovo*)
            if [[ -d /sys/module/thinkpad_acpi ]]; then
                if [[ $(cat /sys/module/thinkpad_acpi/parameters/fan_control 2>/dev/null) == Y ]]; then
                    ok "ThinkPad-Lüftersteuerung ist freigeschaltet"
                elif [[ $THINKPAD == yes ]] || ask "ThinkPad: Lüftersteuerung freischalten (thinkpad_acpi fan_control=1)?" y; then
                    echo "options thinkpad_acpi fan_control=1" > /etc/modprobe.d/fancontrol-linux-thinkpad.conf
                    record /etc/modprobe.d/fancontrol-linux-thinkpad.conf
                    hint "ThinkPad: Lüftersteuerung wird nach einem Neustart aktiv"
                fi
            fi ;;
    esac
    if [[ "${VENDOR,,} ${BOARD,,}" == *asus* ]]; then
        load_module asus_ec_sensors || true
        load_module asus_wmi_sensors || true
    fi
    if [[ -d /sys/class/hwmon ]] && grep -qsE "^(nzxt|corsair|aquacomputer|d5next|octo|quadro|highflownext|kraken)" /sys/class/hwmon/*/name; then
        ok "USB-Kühlgeräte mit Kerneltreiber: $(cat /sys/class/hwmon/*/name | grep -E '^(nzxt|corsair|aquacomputer|d5next|octo|quadro|highflownext|kraken)' | sort -u | tr '\n' ' ')"
    fi

    step "Grafikkarten"
    GPUS=$(lspci -nn 2>/dev/null | grep -E '\[03[0-9a-f]{2}\]' || true)
    if grep -q '\[10de:' <<<"$GPUS"; then
        if [[ -d /sys/module/nvidia ]]; then
            if ldconfig -p 2>/dev/null | grep -q 'libnvidia-ml.so.1'; then
                ok "NVIDIA: Treiber und NVML vorhanden – Lüfter steuerbar"
            else
                hint "NVIDIA: libnvidia-ml fehlt (Teil des NVIDIA-Treiberpakets, z. B. libnvidia-compute-* / nvidia-utils)"
            fi
        elif [[ -d /sys/module/nouveau ]]; then
            hint "NVIDIA: Der freie nouveau-Treiber kann Lüfter kaum steuern. Proprietären Treiber installieren (Ubuntu: sudo ubuntu-drivers install)."
        else
            hint "NVIDIA: kein Treiber geladen"
        fi
    fi
    if grep -q '\[1002:' <<<"$GPUS"; then
        for card in /sys/class/drm/card[0-9]; do
            [[ $(cat "$card/device/vendor" 2>/dev/null) == 0x1002 ]] || continue
            major=$(cat "$card/device/ip_discovery/die/0/GC/0/major" 2>/dev/null || echo 0)
            if [[ -e "$card/device/gpu_od/fan_ctrl/fan_curve" ]]; then
                ok "AMD $(basename "$card"): Overdrive-Lüftersteuerung aktiv"
            elif [[ $major -ge 11 ]]; then
                warn "AMD $(basename "$card") (RDNA3/4): Lüftersteuerung braucht aktiviertes Overdrive (amdgpu.ppfeaturemask)."
                if [[ $AMD_OVERDRIVE == yes ]] || ask "Overdrive aktivieren? (Kernel-Option, neues initramfs, danach Neustart; der Kernel meldet sich dann als 'tainted')" n; then
                    echo "options amdgpu ppfeaturemask=0xffffffff" > /etc/modprobe.d/fancontrol-linux-amdgpu.conf
                    record /etc/modprobe.d/fancontrol-linux-amdgpu.conf
                    if regen_initramfs; then hint "AMD: Overdrive nach dem nächsten Neustart aktiv"
                    else hint "AMD: initramfs konnte nicht neu erzeugt werden – bitte manuell (Details: $LOG)"; fi
                else
                    hint "AMD RDNA3/4: sudo ./install.sh --amd-overdrive schaltet die Lüftersteuerung frei"
                fi
            else
                ok "AMD $(basename "$card"): Steuerung über amdgpu"
            fi
        done
    fi
    if grep -qiE '\[8086:.*(arc|dg2|battlemage)' <<<"$GPUS"; then
        hint "Intel Arc: Temperaturen werden angezeigt, eine Lüftersteuerung bietet der Linux-Treiber bisher nicht"
    fi

    if [[ ${#MODULES[@]} -gt 0 ]]; then
        CONF=/etc/modules-load.d/fancontrol-linux.conf
        mkdir -p /etc/modules-load.d
        { echo "# Von FanControl for Linux geladen"; printf '%s\n' "${MODULES[@]}" | sort -u; } > "$CONF"
        record "$CONF"
        ok "Treiber werden beim Start automatisch geladen ($CONF)"
    fi
    AFTER=$(pwm_count)
    echo "  Steuerbare Mainboard-/GPU-Ausgänge (hwmon): vorher $BEFORE, jetzt $AFTER"
fi

# Tray unter GNOME braucht die AppIndicator-Erweiterung.
if [[ $INSTALL_DEPS -eq 1 ]] && pgrep -x gnome-shell >/dev/null 2>&1; then
    step "GNOME: Tray-Unterstützung"
    if install_role appindicator "AppIndicator-Erweiterung"; then
        hint "GNOME: Erweiterung 'AppIndicator and KStatusNotifierItem Support' in der Erweiterungen-App aktivieren (nach Neuanmeldung)"
    fi
fi

# --- Dienst -------------------------------------------------------------------------
if [[ $WITH_SERVICE -eq 1 ]]; then
    step "Hintergrunddienst ($INIT)"
    case "$INIT" in
        systemd|systemd-offline)
            install -Dm 644 "$SRC/data/fancontrol-linux.service" /etc/systemd/system/fancontrol-linux.service
            if [[ $INIT == systemd ]]; then
                systemctl daemon-reload
                if systemctl is-active --quiet fancontrol.service; then
                    warn "lm-sensors-Dienst 'fancontrol' wird deaktiviert (beide würden sich stören)"
                    systemctl disable --now fancontrol.service || true
                fi
                systemctl enable fancontrol-linux.service >>"$LOG" 2>&1
                systemctl restart fancontrol-linux.service
                sleep 1
                if systemctl is-active --quiet fancontrol-linux.service; then ok "Dienst läuft"
                else fail "Dienst startet nicht – journalctl -u fancontrol-linux zeigt die Ursache"; fi
            else
                systemctl enable fancontrol-linux.service >>"$LOG" 2>&1 || true
                warn "systemd läuft hier nicht (Container?) – Dienst ist eingerichtet und startet beim nächsten Boot"
            fi ;;
        openrc)
            install -Dm 755 "$SRC/data/openrc/fancontrol-linux" /etc/init.d/fancontrol-linux
            rc-update add fancontrol-linux default >>"$LOG" 2>&1 || true
            rc-service fancontrol-linux restart >>"$LOG" 2>&1 && ok "Dienst läuft" || warn "Dienst konnte nicht gestartet werden (Details: $LOG)" ;;
        runit)
            install -Dm 755 "$SRC/data/runit/run" /etc/sv/fancontrol-linux/run
            ln -sfn /etc/sv/fancontrol-linux /var/service/fancontrol-linux 2>/dev/null \
                || ln -sfn /etc/sv/fancontrol-linux /etc/runit/runsvdir/default/fancontrol-linux 2>/dev/null || true
            ok "runit-Dienst eingerichtet" ;;
        *)
            hint "Kein unterstütztes Init-System gefunden – Dienst manuell starten: sudo fancontrol-linuxd" ;;
    esac
fi

# --- Zusammenfassung ------------------------------------------------------------------
step "Erkannte Hardware"
if [[ -n $APPIMAGE_SRC ]]; then SUMMARY=("$OPT/AppRun" --python -); else SUMMARY=(env PYTHONPATH="$LIBDIR" python3 -); fi
"${SUMMARY[@]}" <<'EOF' 2>>"$LOG" || warn "Übersicht nicht verfügbar (Details: $LOG)"
from fancontrol_linux.hwmon import Hardware
marks = {"ok": "✓", "warn": "!", "off": "-"}
for row in Hardware().info():
    print(f"  {marks.get(row['state'], '?')} {row['name']}: {row['detail']}")
EOF

echo
if [[ ${#HINTS[@]} -gt 0 ]]; then
    echo "${B}Hinweise:${N}"
    printf '  • %s\n' "${HINTS[@]}"
    echo
fi
echo "${G}Fertig.${N} Starte 'FanControl' aus dem Anwendungsmenü oder mit: fancontrol-linux"
echo "Protokoll: $LOG"
if [[ $RELOGIN -eq 1 ]]; then
    echo "${B}WICHTIG:${N} '$TARGET_USER' wurde zur Gruppe 'fancontrol' hinzugefügt – bitte einmal ab- und wieder anmelden."
fi
