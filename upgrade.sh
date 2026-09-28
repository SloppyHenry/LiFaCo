#!/usr/bin/env bash
# Linux FanControl – upgrade an existing installation to the latest (or a given) release from GitHub.
# Works for both installation types: from source (install.sh) and from the AppImage (--install).
# Configuration, profiles and driver settings are kept.
set -euo pipefail

REPO="SloppyHenry/FanControlLinux"
API="https://api.github.com/repos/$REPO/releases"
PREFIX=/usr/local
OPT=/opt/fancontrol-linux
LIBDIR="$PREFIX/lib/fancontrol-linux"
APPIMAGE_NAME="LinuxFanControl-x86_64.AppImage"

CHECK_ONLY=0
FORCE=0
WANTED=""

usage() {
    cat <<'EOF'
Usage: sudo fancontrol-linux-upgrade [options]

  --check              Only check whether an update is available (no root needed)
  --version vX.Y.Z     Install this release instead of the latest one (also allows downgrades)
  --force              Reinstall even if the installed version is up to date
  -h, --help           This help

Configuration, profiles and driver settings are kept. Hardware detection is not repeated;
run the installer again if you want that (sudo ./install.sh or the AppImage with --install).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --force) FORCE=1 ;;
        --version) WANTED=${2:?--version needs a tag, e.g. v1.1.0}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

if [[ $CHECK_ONLY -eq 0 && $EUID -ne 0 ]]; then
    echo "Please run with sudo: sudo fancontrol-linux-upgrade" >&2
    exit 1
fi

fetch() {   # fetch <url> [output file]
    if command -v curl >/dev/null; then
        if [[ $# -eq 2 ]]; then curl -fsSL -o "$2" "$1"; else curl -fsSL "$1"; fi
    elif command -v wget >/dev/null; then
        if [[ $# -eq 2 ]]; then wget -qO "$2" "$1"; else wget -qO- "$1"; fi
    else
        echo "curl or wget is required" >&2
        exit 1
    fi
}

version_of() {   # version_of <path to fancontrol_linux/__init__.py>
    sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1" 2>/dev/null
}

# --- What is installed? ------------------------------------------------------------
if [[ -x $OPT/AppRun ]]; then
    MODE=appimage
    INSTALLED=$(version_of "$OPT/usr/lib/fancontrol-linux/fancontrol_linux/__init__.py")
elif [[ -d $LIBDIR/fancontrol_linux ]]; then
    MODE=source
    INSTALLED=$(version_of "$LIBDIR/fancontrol_linux/__init__.py")
else
    echo "Linux FanControl does not seem to be installed (neither $OPT nor $LIBDIR exists)." >&2
    echo "Install it first: sudo ./install.sh, or sudo ./$APPIMAGE_NAME --install" >&2
    exit 1
fi
INSTALLED=${INSTALLED:-0}

# --- Which release? ----------------------------------------------------------------
if [[ -n $WANTED ]]; then RELEASE_URL="$API/tags/$WANTED"; else RELEASE_URL="$API/latest"; fi
if ! RELEASE=$(fetch "$RELEASE_URL"); then
    echo "Could not read release information from GitHub ($RELEASE_URL)." >&2
    exit 1
fi
TAG=$(grep -o '"tag_name": *"[^"]*"' <<<"$RELEASE" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
if [[ -z $TAG ]]; then
    echo "No release found${WANTED:+ for $WANTED}." >&2
    exit 1
fi
AVAILABLE=${TAG#v}

echo "Installed: $INSTALLED ($MODE installation)"
echo "Available: $AVAILABLE"

NEWEST=$(printf '%s\n%s\n' "$AVAILABLE" "$INSTALLED" | sort -V | tail -1)
if [[ -z $WANTED && $FORCE -eq 0 && ( $AVAILABLE == "$INSTALLED" || $NEWEST == "$INSTALLED" ) ]]; then
    echo "Linux FanControl is up to date."
    exit 0
fi
if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "An update is available: sudo fancontrol-linux-upgrade"
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

asset_url() {   # asset_url <file name>
    grep -o '"browser_download_url": *"[^"]*"' <<<"$RELEASE" | sed 's/.*"\([^"]*\)"$/\1/' | grep "/$1\$" | head -1 || true
}

if [[ $MODE == appimage ]]; then
    URL=$(asset_url "$APPIMAGE_NAME")
    if [[ -z $URL ]]; then
        echo "Release $TAG has no $APPIMAGE_NAME." >&2
        exit 1
    fi
    echo "Downloading $APPIMAGE_NAME ($TAG) …"
    fetch "$URL" "$TMP/$APPIMAGE_NAME"
    SUM_URL=$(asset_url "$APPIMAGE_NAME.sha256")
    if [[ -n $SUM_URL ]]; then
        fetch "$SUM_URL" "$TMP/$APPIMAGE_NAME.sha256"
        (cd "$TMP" && sha256sum -c "$APPIMAGE_NAME.sha256" >/dev/null) || { echo "Checksum mismatch – aborting." >&2; exit 1; }
        echo "Checksum verified."
    fi
    chmod +x "$TMP/$APPIMAGE_NAME"
    # Extract-and-run avoids needing FUSE as root.
    APPIMAGE_EXTRACT_AND_RUN=1 "$TMP/$APPIMAGE_NAME" --install --yes --no-hardware
else
    echo "Downloading source $TAG …"
    fetch "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" "$TMP/source.tar.gz"
    tar -xzf "$TMP/source.tar.gz" -C "$TMP"
    SRC_DIR=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
    bash "$SRC_DIR/install.sh" --yes --no-hardware
fi

echo
echo "Linux FanControl updated from $INSTALLED to $AVAILABLE."
echo "If the user interface is open, close it (also in the tray) and start it again."
