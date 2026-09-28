"""Hardware access: hwmon sysfs, AMD overdrive, NVIDIA (NVML) and USB backends (liquidctl, Thermaltake)."""

import os
import re
from dataclasses import dataclass, field

from . import amdgpu, liquidctl_backend, naming, nvidia, thermaltake

PWM_MAX = 255
PWM_MODE_MANUAL = 1


def hwmon_root():
    return os.environ.get("FANCONTROL_HWMON_ROOT", "/sys/class/hwmon")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path):
    value = _read(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


@dataclass
class TempSensor:
    id: str
    label: str
    path: str

    def read(self):
        raw = _read_int(self.path)
        return None if raw is None else raw / 1000.0


@dataclass
class FanSensor:
    id: str
    label: str
    path: str

    def read(self):
        return _read_int(self.path)


@dataclass
class PwmOutput:
    id: str
    label: str
    pwm_path: str
    enable_path: str | None
    default_fan: str | None = None
    _original_enable: int | None = field(default=None, repr=False)
    _original_pwm: int | None = field(default=None, repr=False)
    _taken: bool = field(default=False, repr=False)

    def read_percent(self):
        raw = _read_int(self.pwm_path)
        return None if raw is None else round(raw * 100 / PWM_MAX, 1)

    def take_control(self):
        """Switch to manual mode, remembering the original state for restore()."""
        if not self._taken:
            self._original_enable = _read_int(self.enable_path) if self.enable_path else None
            self._original_pwm = _read_int(self.pwm_path)
            self._taken = True
        if self.enable_path and _read_int(self.enable_path) != PWM_MODE_MANUAL:
            _write(self.enable_path, PWM_MODE_MANUAL)

    def write_percent(self, percent):
        percent = max(0.0, min(100.0, percent))
        _write(self.pwm_path, int(round(percent * PWM_MAX / 100)))

    @property
    def controlled(self):
        return self._taken

    def restore(self):
        if not self._taken:
            return
        try:
            if self.enable_path and self._original_enable is not None and self._original_enable != PWM_MODE_MANUAL:
                _write(self.enable_path, self._original_enable)
            elif self._original_pwm:
                _write(self.pwm_path, self._original_pwm)
            else:
                # No automatic mode and no running state to go back to: full speed is the safe choice.
                _write(self.pwm_path, PWM_MAX)
        except OSError:
            try:
                _write(self.pwm_path, PWM_MAX)
            except OSError:
                pass
        self._taken = False


def _device_key(hwmon_dir, name, seen):
    """A key that survives reboots, unlike the hwmonN index."""
    device = os.path.join(hwmon_dir, "device")
    key = None
    if os.path.exists(device):
        real = os.path.realpath(device)
        key = real.split("/sys/devices/", 1)[-1] if "/sys/devices/" in real else os.path.basename(real)
    if not key:
        key = name
    base = f"{name}@{key}" if key != name else name
    result, n = base, 2
    while result in seen:
        result = f"{base}#{n}"
        n += 1
    seen.add(result)
    return result


def _numbered(entries, prefix, suffix):
    pattern = re.compile(rf"^{prefix}(\d+){suffix}$")
    found = []
    for entry in entries:
        m = pattern.match(entry)
        if m:
            found.append(int(m.group(1)))
    return sorted(found)


def _dmi(field):
    return (_read(os.path.join(os.environ.get("FANCONTROL_DMI_ROOT", "/sys/class/dmi/id"), field)) or "").strip()


def _module_loaded(name):
    return os.path.isdir(f"/sys/module/{name}")


class Hardware:
    """All sensors and fan outputs. hwmon and NVIDIA are rescanned cheaply; USB backends only on full scans."""

    def __init__(self, root=None, nvidia=None, settings=None):
        self.root = root or hwmon_root()
        # The demo sets FANCONTROL_NO_NVIDIA so simulated runs never touch a real GPU.
        self.nvidia_enabled = os.environ.get("FANCONTROL_NO_NVIDIA") != "1" if nvidia is None else nvidia
        self.settings = dict(settings or {})
        self.temps, self.fans, self.pwms = {}, {}, {}
        self.usb = ({}, {}, {})  # temps, fans, outputs from USB backends
        self.liquidctl = liquidctl_backend.LiquidctlBackend()
        self.thermaltake = thermaltake.ThermaltakeBackend()
        self.amd_od = {}
        self.chips = []
        self.scan(full=True)

    def configure(self, settings):
        keys = ("liquidctl", "thermaltake")
        changed = any(bool(self.settings.get(k)) != bool(settings.get(k)) for k in keys)
        self.settings = dict(settings)
        if changed:
            self.scan(full=True)

    def _scan_usb(self):
        for out in self.usb[2].values():
            out.restore()
        self.liquidctl.close()
        self.thermaltake.close()
        temps, fans, outputs = {}, {}, {}
        if self.settings.get("liquidctl", True) and liquidctl_backend.available():
            self.liquidctl.scan(temps, fans, outputs)
        if self.settings.get("thermaltake", False):
            self.thermaltake.scan(fans, outputs)
        self.usb = (temps, fans, outputs)

    def scan(self, full=False):
        if full:
            self._scan_usb()
        old_pwms = self.pwms
        self.temps, self.fans, self.pwms = {}, {}, {}
        self.amd_od, self.chips = {}, []
        seen = set()
        device_counts = {}
        try:
            dirs = sorted(os.listdir(self.root), key=lambda d: int(re.sub(r"\D", "", d) or 0))
        except OSError:
            dirs = []
        for d in dirs:
            path = os.path.join(self.root, d)
            name = _read(os.path.join(path, "name")) or d
            key = _device_key(path, name, seen)
            self.chips.append(name)
            device = naming.device_name(path, name)
            device_counts[device] = device_counts.get(device, 0) + 1
            if device_counts[device] > 1:
                device = f"{device} #{device_counts[device]}"
            try:
                entries = os.listdir(path)
            except OSError:
                continue
            for n in _numbered(entries, "temp", "_input"):
                label = naming.channel_label(name, "temp", n, _read(os.path.join(path, f"temp{n}_label")))
                sid = f"{key}:temp{n}"
                self.temps[sid] = TempSensor(sid, f"{device}: {label}", os.path.join(path, f"temp{n}_input"))
            for n in _numbered(entries, "fan", "_input"):
                label = naming.channel_label(name, "fan", n, _read(os.path.join(path, f"fan{n}_label")))
                sid = f"{key}:fan{n}"
                self.fans[sid] = FanSensor(sid, f"{device}: {label}", os.path.join(path, f"fan{n}_input"))
            od = amdgpu.od_status(path) if name == "amdgpu" else None
            if od:
                self.amd_od[key] = od
            if od == "active":
                sid = f"{key}:pwm1"
                fan = f"{key}:fan1"
                self.pwms[sid] = amdgpu.AmdOdOutput(sid, f"{device}: fan control (overdrive)", path,
                                                    fan if fan in self.fans else None)
                continue
            if od == "disabled":
                continue  # pwm1 is read-only on RDNA3+ without overdrive
            for n in _numbered(entries, "pwm", ""):
                sid = f"{key}:pwm{n}"
                enable = os.path.join(path, f"pwm{n}_enable")
                fan = f"{key}:fan{n}"
                self.pwms[sid] = PwmOutput(
                    sid, f"{device}: {naming.channel_label(name, 'pwm', n, None)}", os.path.join(path, f"pwm{n}"),
                    enable if os.path.exists(enable) else None,
                    fan if fan in self.fans else None,
                )
        if self.nvidia_enabled:
            nvidia.scan(self.temps, self.fans, self.pwms)
        usb_temps, usb_fans, usb_outputs = self.usb
        self.temps.update(usb_temps)
        self.fans.update(usb_fans)
        self.pwms.update(usb_outputs)
        for sid, prev in old_pwms.items():
            if sid in usb_outputs:
                continue
            if sid in self.pwms and prev.controlled:
                self.pwms[sid] = prev  # keep the object that remembers the original firmware state
            elif sid not in self.pwms:
                # Controls that vanished must still be handed back to the firmware.
                prev.restore()

    def temp_labels(self):
        return {sid: s.label for sid, s in self.temps.items()}

    def read_temps(self):
        return {sid: s.read() for sid, s in self.temps.items()}

    def read_fans(self):
        return {sid: s.read() for sid, s in self.fans.items()}

    def restore_all(self):
        for pwm in self.pwms.values():
            pwm.restore()
        self.liquidctl.close()
        self.thermaltake.close()

    def info(self):
        """Support overview for the settings page: [{name, state, detail, hint}], state in ok/warn/off."""
        rows = []
        hw_pwms = [p for p in self.pwms.values() if isinstance(p, PwmOutput)]
        board = " ".join(x for x in (_dmi("board_vendor"), _dmi("board_name")) if x)
        chips = sorted(set(self.chips))
        rows.append({
            "name": "Mainboard / hwmon",
            "state": "ok" if hw_pwms else "warn",
            "detail": f"{board or 'Unknown board'} – chips: {', '.join(chips) or 'none'} – "
                      f"{len(hw_pwms)} controllable outputs",
            "hint": "" if hw_pwms else "No controllable mainboard fans: run sudo sensors-detect or run the "
                                         "installer again (it loads matching drivers).",
        })
        vendor = _dmi("sys_vendor").lower()
        if "dell" in vendor:
            loaded = "dell_smm" in chips or _module_loaded("dell_smm_hwmon")
            rows.append({"name": "Dell", "state": "ok" if loaded else "warn",
                         "detail": "dell-smm-hwmon loaded" if loaded else "dell-smm-hwmon not loaded",
                         "hint": "" if loaded else "sudo modprobe dell-smm-hwmon (possibly with ignore_dmi=1)"})
        if "lenovo" in vendor and _module_loaded("thinkpad_acpi"):
            enabled = (_read("/sys/module/thinkpad_acpi/parameters/fan_control") or "").strip() in ("Y", "1")
            rows.append({"name": "ThinkPad", "state": "ok" if enabled else "warn",
                         "detail": "Fan control enabled" if enabled else "fan_control=0",
                         "hint": "" if enabled else "The installer can set 'options thinkpad_acpi fan_control=1' "
                                                    "(reboot afterwards)."})
        if "asus" in vendor or "asustek" in board.lower():
            ec = "asusec" in chips or "asus_wmi_sensors" in chips
            rows.append({"name": "ASUS", "state": "ok" if ec else "off",
                         "detail": "Additional ASUS sensors active" if ec else "asus-ec-sensors not active",
                         "hint": "" if ec else "For additional sensors: sudo modprobe asus-ec-sensors"})
        nvml = nvidia.Nvml.get() if self.nvidia_enabled else None
        nv_out = [p for p in self.pwms.values() if isinstance(p, nvidia.NvidiaFanOutput)]
        if self.nvidia_enabled and (nvml or _module_loaded("nvidia") or "nouveau" in chips):
            rows.append({
                "name": "NVIDIA",
                "state": "ok" if nvml else "warn",
                "detail": f"NVML active – {len(nv_out)} controllable fans" if nvml else
                          ("nouveau driver (hardly any fan control)" if "nouveau" in chips else "NVML not found"),
                "hint": "" if nvml else "Install the proprietary NVIDIA driver (it includes libnvidia-ml).",
            })
        for key, state in self.amd_od.items():
            rows.append({
                "name": "AMD Radeon (RDNA3/4)",
                "state": "ok" if state == "active" else "warn",
                "detail": "Overdrive fan curve active" if state == "active" else "Overdrive disabled – monitoring only",
                "hint": "" if state == "active" else "Set the kernel parameter amdgpu.ppfeaturemask=0xffffffff "
                                                     "(the installer offers this), then reboot.",
            })
        if "amdgpu" in chips and not self.amd_od:
            rows.append({"name": "AMD Radeon", "state": "ok", "detail": "Controlled through amdgpu (pwm1)", "hint": ""})
        if any(c in chips for c in ("i915", "xe")):
            rows.append({"name": "Intel Arc / Intel graphics", "state": "off",
                         "detail": "Temperatures/fan speed are shown",
                         "hint": "The Linux driver does not (yet) allow fan control for Intel GPUs."})
        kernel_devices = [c for c in chips if c.startswith(("nzxt", "corsair", "aquacomputer", "d5next", "octo",
                                                              "quadro", "highflownext", "farbwerk", "kraken"))]
        if kernel_devices:
            rows.append({"name": "USB devices (kernel drivers)", "state": "ok", "detail": ", ".join(kernel_devices),
                         "hint": ""})
        if not self.settings.get("liquidctl", True):
            rows.append({"name": "liquidctl (AIO/smart devices)", "state": "off", "detail": "Disabled", "hint": ""})
        elif not liquidctl_backend.available():
            rows.append({"name": "liquidctl (AIO/smart devices)", "state": "off", "detail": "Not installed",
                         "hint": "Install the liquidctl package (the installer does this)."})
        else:
            rows.append({"name": "liquidctl (AIO/smart devices)", "state": "ok" if self.liquidctl.devices else "off",
                         "detail": self.liquidctl.info(), "hint": ""})
        rows.append({"name": "Thermaltake (experimental)",
                     "state": "ok" if self.thermaltake.controllers else "off",
                     "detail": self.thermaltake.info() if self.settings.get("thermaltake") else "Disabled",
                     "hint": ""})
        return rows
