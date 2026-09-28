"""Human-readable names for hwmon devices and their channels.

The kernel only exposes driver names such as "it8689_61030507", "k10temp" or "pwm1". Real names come from
/proc/cpuinfo (CPU model), the PCI ID database (graphics, network and Wi-Fi cards), device model files (SSDs,
disks) and a table of well-known sensor chips and labels. Stable sensor IDs are not affected.
"""

import os
import re

PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
VENDOR_SHORT = {
    "1002": "AMD", "1022": "AMD", "10de": "NVIDIA", "8086": "Intel", "10ec": "Realtek", "14c3": "MediaTek",
    "144d": "Samsung", "1987": "Phison", "15b7": "SanDisk", "c0a9": "Crucial", "1c5c": "SK hynix",
    "2646": "Kingston", "1179": "Toshiba", "14e4": "Broadcom", "168c": "Qualcomm Atheros", "17cb": "Qualcomm",
}
CPU_CHIPS = ("k10temp", "zenpower", "coretemp", "cpu_thermal")
# Super I/O chip families: name prefix -> vendor.
SUPER_IO = (("it8", "ITE"), ("it87", "ITE"), ("nct6", "Nuvoton"), ("nct7", "Nuvoton"), ("f718", "Fintek"),
            ("f719", "Fintek"), ("f71", "Fintek"), ("w83", "Winbond"), ("sch56", "SMSC"))
MAINBOARD_OTHER = {
    "gigabyte_wmi": "Mainboard (Gigabyte WMI)",
    "asusec": "Mainboard (ASUS EC)",
    "asus_ec_sensors": "Mainboard (ASUS EC)",
    "asus_wmi_sensors": "Mainboard (ASUS WMI)",
    "acpitz": "ACPI thermal zone",
    "dell_smm": "Dell",
    "thinkpad": "ThinkPad",
    "corsaircpro": "Corsair Commander Pro",
    "corsairpsu": "Corsair PSU",
    "kraken3": "NZXT Kraken",
    "nzxtsmart2": "NZXT Smart Device",
}
LABELS = {
    # AMD CPUs
    "Tctl": "Package (Tctl)", "Tdie": "Die (Tdie)",
    # Nuvoton Super I/O
    "SYSTIN": "System", "CPUTIN": "CPU socket", "PECI Agent 0": "CPU (PECI)", "PECI Agent 1": "CPU 2 (PECI)",
    "PCH_CHIP_TEMP": "Chipset", "PCH_CHIP_CPU_MAX_TEMP": "Chipset (CPU max)", "PCH_CPU_TEMP": "CPU (PCH)",
    "PCH_MCH_TEMP": "Chipset (MCH)", "SMBUSMASTER 0": "CPU (SMBus)",
    # AMD GPUs
    "edge": "Edge", "junction": "Hotspot", "mem": "Memory",
}

_pci_cache = {}
_cpu_cache = {}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_model():
    if "model" not in _cpu_cache:
        model = None
        text = _read(os.environ.get("FANCONTROL_CPUINFO", "/proc/cpuinfo")) or ""
        m = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
        if m:
            model = m.group(1)
            model = re.sub(r"\((R|TM|tm|r)\)", "", model)
            model = re.sub(r"\s+\d+-Core Processor|\s+Processor|\s+CPU\s*@.*$|\s+with Radeon.*$", "", model)
            model = re.sub(r"\s+", " ", model).strip()
        _cpu_cache["model"] = model
    return _cpu_cache["model"]


def _pci_ids_path():
    override = os.environ.get("FANCONTROL_PCI_IDS")
    for path in ((override,) if override else PCI_IDS):
        if path and os.path.exists(path):
            return path
    return None


def pci_name(vendor, device):
    """'AMD Radeon RX 7600' style name from the PCI ID database, or None."""
    vendor, device = vendor.lower().removeprefix("0x"), device.lower().removeprefix("0x")
    key = (vendor, device)
    if key in _pci_cache:
        return _pci_cache[key]
    path = _pci_ids_path()
    vendor_name = device_name = None
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if not line or line[0] == "#":
                        continue
                    if line[0] not in "\t":
                        if in_vendor:
                            break
                        if line.startswith(vendor + "  "):
                            in_vendor = True
                            vendor_name = line[6:].strip()
                    elif in_vendor and not line.startswith("\t\t") and line[1:5] == device:
                        device_name = line[7:].strip()
                        break
        except OSError:
            pass
    name = None
    if device_name:
        bracket = re.search(r"\[(.+)\]", device_name)
        model = bracket.group(1).split("/")[0].strip() if bracket else device_name
        short = VENDOR_SHORT.get(vendor) or (vendor_name.split()[0] if vendor_name else "")
        name = f"{short} {model}".strip() if not model.lower().startswith(short.lower()) else model
    _pci_cache[key] = name
    return name


def _pci_parent(path):
    """Nearest PCI device directory at or above a sysfs device path."""
    while path and path != "/":
        if os.path.exists(os.path.join(path, "vendor")) and os.path.exists(os.path.join(path, "class")):
            return path
        path = os.path.dirname(path)
    return None


def device_name(hwmon_dir, chip):
    """Readable name for one hwmon device, e.g. 'AMD Ryzen 9 5950X' or 'Mainboard (ITE IT8689)'."""
    base = chip.split("_")[0] if not chip.startswith(tuple(MAINBOARD_OTHER)) else chip
    if chip in CPU_CHIPS:
        return cpu_model() or "CPU"
    for prefix, vendor in SUPER_IO:
        if chip.startswith(prefix):
            return f"Mainboard ({vendor} {base.upper()})"
    for key, name in MAINBOARD_OTHER.items():
        if chip.startswith(key):
            return name
    device = os.path.join(hwmon_dir, "device")
    real = os.path.realpath(device) if os.path.exists(device) else None
    model = _read(os.path.join(device, "model")) if real else None
    if chip == "nvme":
        return f"NVMe {model}" if model else "NVMe SSD"
    if chip == "drivetemp":
        return f"Disk {model}" if model else "Disk"
    pci = _pci_parent(real) if real else None
    if pci:
        vendor, dev = _read(os.path.join(pci, "vendor")), _read(os.path.join(pci, "device"))
        if vendor and dev:
            name = pci_name(vendor, dev)
            if name:
                return name
    return chip


def channel_label(chip, kind, index, raw_label):
    """Readable channel name: kernel label if there is one (made friendlier), else 'Fan 2', 'Fan header 1' …"""
    if raw_label:
        if raw_label in LABELS:
            return LABELS[raw_label]
        m = re.fullmatch(r"Tccd(\d+)", raw_label)
        if m:
            return f"CCD {m.group(1)}"
        m = re.fullmatch(r"AUXTIN(\d+)", raw_label)
        if m:
            return f"Auxiliary {int(m.group(1)) + 1}"
        if raw_label == "Package id 0":
            return "Package"
        return raw_label
    if kind == "temp":
        return "Temperature" if chip in ("nvme", "drivetemp") or chip.startswith(("r8169", "mt79", "iwlwifi")) \
            else f"Temperature {index}"
    if kind == "fan":
        return "Fan" if chip == "amdgpu" else f"Fan {index}"
    if kind == "pwm":
        return "Fan control" if chip == "amdgpu" else f"Fan header {index}"
    return f"{kind}{index}"

