"""NVIDIA GPUs via NVML (libnvidia-ml): temperatures, fan speeds and fan control (control needs root)."""

import ctypes
import shutil
import subprocess

NVML_SUCCESS = 0


class _FanSpeedInfo(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]


class Nvml:
    _instance = None

    @classmethod
    def get(cls):
        """Shared NVML handle, or None when no NVIDIA driver is present."""
        if cls._instance is None:
            try:
                cls._instance = cls()
            except OSError:
                cls._instance = False
        return cls._instance or None

    def __init__(self):
        self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        if self.lib.nvmlInit_v2() != NVML_SUCCESS:
            raise OSError("nvmlInit failed")

    def _call(self, name, *args):
        fn = getattr(self.lib, name, None)
        if fn is None:
            raise OSError(f"{name} not available")
        rc = fn(*args)
        if rc != NVML_SUCCESS:
            raise OSError(f"{name}: NVML error {rc}")

    def devices(self):
        count = ctypes.c_uint()
        self._call("nvmlDeviceGetCount_v2", ctypes.byref(count))
        result = []
        for i in range(count.value):
            handle = ctypes.c_void_p()
            self._call("nvmlDeviceGetHandleByIndex_v2", i, ctypes.byref(handle))
            result.append(handle)
        return result

    def name(self, h):
        buf = ctypes.create_string_buffer(96)
        self._call("nvmlDeviceGetName", h, buf, 96)
        return buf.value.decode(errors="replace")

    def temperature(self, h):
        t = ctypes.c_uint()
        self._call("nvmlDeviceGetTemperature", h, 0, ctypes.byref(t))
        return float(t.value)

    def num_fans(self, h):
        n = ctypes.c_uint()
        try:
            self._call("nvmlDeviceGetNumFans", h, ctypes.byref(n))
        except OSError:
            return 0
        return n.value

    def fan_percent(self, h, fan):
        s = ctypes.c_uint()
        self._call("nvmlDeviceGetFanSpeed_v2", h, fan, ctypes.byref(s))
        return float(s.value)

    def fan_rpm(self, h, fan):
        info = _FanSpeedInfo(version=ctypes.sizeof(_FanSpeedInfo) | (1 << 24), fan=fan)
        self._call("nvmlDeviceGetFanSpeedRPM", h, ctypes.byref(info))
        return int(info.speed)

    def fan_limits(self, h):
        lo, hi = ctypes.c_uint(), ctypes.c_uint()
        try:
            self._call("nvmlDeviceGetMinMaxFanSpeed", h, ctypes.byref(lo), ctypes.byref(hi))
        except OSError:
            return 0, 100
        return lo.value, hi.value

    def set_fan(self, h, fan, percent):
        self._call("nvmlDeviceSetFanSpeed_v2", h, fan, int(round(percent)))

    def set_default(self, h, fan):
        self._call("nvmlDeviceSetDefaultFanSpeed_v2", h, fan)


class NvidiaTemp:
    def __init__(self, sid, label, nvml, handle):
        self.id, self.label, self.nvml, self.handle = sid, label, nvml, handle

    def read(self):
        try:
            return self.nvml.temperature(self.handle)
        except OSError:
            return None


class NvidiaSmiTemp:
    """Fallback when libnvidia-ml cannot be loaded but nvidia-smi exists."""

    def __init__(self, sid, label, index):
        self.id, self.label, self.index = sid, label, index

    def read(self):
        try:
            res = subprocess.run(["nvidia-smi", "-i", str(self.index), "--query-gpu=temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
            return float(res.stdout.strip()) if res.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None


class NvidiaFanRpm:
    def __init__(self, sid, label, nvml, handle, fan):
        self.id, self.label, self.nvml, self.handle, self.fan = sid, label, nvml, handle, fan

    def read(self):
        try:
            return self.nvml.fan_rpm(self.handle, self.fan)
        except OSError:
            return None


class NvidiaFanOutput:
    """A GPU fan. Setting 0 % hands the fan back to the driver, which stops it when the GPU is cool."""

    pwm_path = None
    enable_path = None

    def __init__(self, sid, label, nvml, handle, fan, default_fan):
        self.id, self.label, self.nvml, self.handle, self.fan = sid, label, nvml, handle, fan
        self.default_fan = default_fan
        self.min_percent, self.max_percent = nvml.fan_limits(handle)
        self._taken = False
        self._auto = False

    @property
    def controlled(self):
        return self._taken

    def read_percent(self):
        try:
            return self.nvml.fan_percent(self.handle, self.fan)
        except OSError:
            return None

    def take_control(self):
        self._taken = True

    def write_percent(self, percent):
        if percent <= 0:
            if not self._auto:
                self.nvml.set_default(self.handle, self.fan)
                self._auto = True
            return
        self._auto = False
        self.nvml.set_fan(self.handle, self.fan, max(self.min_percent, min(self.max_percent, percent)))

    def restore(self):
        if not self._taken:
            return
        try:
            self.nvml.set_default(self.handle, self.fan)
        except OSError:
            pass
        self._taken = False
        self._auto = False


def scan(temps, fans, outputs):
    """Adds NVIDIA sensors and fan outputs to the given dicts."""
    nvml = Nvml.get()
    if nvml is None:
        if shutil.which("nvidia-smi"):
            try:
                res = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                return
            for line in res.stdout.strip().splitlines():
                idx, _, name = line.partition(",")
                if idx.strip().isdigit():
                    sid = f"nvidia:{idx.strip()}:temp"
                    temps[sid] = NvidiaSmiTemp(sid, f"{_gpu_label(name.strip())}: GPU", int(idx))
        return
    try:
        handles = nvml.devices()
    except OSError:
        return
    for i, h in enumerate(handles):
        try:
            name = _gpu_label(nvml.name(h))
        except OSError:
            name = f"NVIDIA GPU {i}"
        sid = f"nvidia:{i}:temp"
        temps[sid] = NvidiaTemp(sid, f"{name}: GPU", nvml, h)
        for f in range(nvml.num_fans(h)):
            rpm_id = f"nvidia:{i}:fan{f}"
            rpm = NvidiaFanRpm(rpm_id, f"{name}: Fan {f + 1}", nvml, h, f)
            has_rpm = rpm.read() is not None
            if has_rpm:
                fans[rpm_id] = rpm
            out_id = f"nvidia:{i}:pwm{f}"
            outputs[out_id] = NvidiaFanOutput(out_id, f"{name}: Fan {f + 1}", nvml, h, f,
                                              rpm_id if has_rpm else None)


def _gpu_label(name):
    return name if name.upper().startswith("NVIDIA") else f"NVIDIA {name}"
