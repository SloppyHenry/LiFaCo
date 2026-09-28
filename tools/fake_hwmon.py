#!/usr/bin/env python3
"""Simulated hwmon tree (Super-I/O chip, CPU, GPU) for trying the app without controllable fans.

Usage: fake_hwmon.py DIR   – creates DIR/hwmonN and keeps simulating until interrupted.
"""

import math
import os
import sys
import time

DEVICES = {
    "hwmon0": {"name": "nct6798",
               "temps": {1: "SYSTIN", 2: "CPUTIN", 3: "AUXTIN0"},
               "fans": {1: 1800, 2: 1400, 3: 2200},
               "pwms": {1: 5, 2: 5, 3: 5}},
    "hwmon1": {"name": "k10temp", "temps": {1: "Tctl", 3: "Tccd1"}, "fans": {}, "pwms": {}},
    "hwmon2": {"name": "amdgpu", "temps": {1: "edge", 2: "junction"},
               "fans": {1: 3300}, "pwms": {1: 2}},
}
STOP_BELOW = 0.18
START_ABOVE = 0.25


def write(path, value):
    with open(path, "w") as f:
        f.write(f"{value}\n")


def read_int(path, default=0):
    try:
        with open(path) as f:
            return int(f.read().strip() or default)
    except (OSError, ValueError):
        return default


def create(root):
    for d, dev in DEVICES.items():
        p = os.path.join(root, d)
        os.makedirs(p, exist_ok=True)
        write(os.path.join(p, "name"), dev["name"])
        for n, label in dev["temps"].items():
            write(os.path.join(p, f"temp{n}_input"), 40000)
            write(os.path.join(p, f"temp{n}_label"), label)
        for n in dev["fans"]:
            write(os.path.join(p, f"fan{n}_input"), 0)
        for n, mode in dev["pwms"].items():
            write(os.path.join(p, f"pwm{n}"), 128)
            write(os.path.join(p, f"pwm{n}_enable"), mode)


def simulate(root):
    spinning = {}
    cpu, gpu = 45.0, 40.0
    t0 = time.time()
    while True:
        t = time.time() - t0
        load = 0.5 + 0.5 * math.sin(t / 25)
        gpu_load = 0.5 + 0.5 * math.sin(t / 40 + 1)
        duty = {}
        for d, dev in DEVICES.items():
            for n, max_rpm in dev["fans"].items():
                base = os.path.join(root, d)
                pwm_file = os.path.join(base, f"pwm{n}")
                mode = read_int(os.path.join(base, f"pwm{n}_enable"), 1)
                temp = gpu if dev["name"] == "amdgpu" else cpu
                if mode != 1:
                    write(pwm_file, int(255 * min(1.0, max(0.3, (temp - 30) / 50))))
                frac = read_int(pwm_file, 128) / 255
                key = (d, n)
                if spinning.get(key, True):
                    spinning[key] = frac >= STOP_BELOW
                else:
                    spinning[key] = frac >= START_ABOVE
                rpm = int(max_rpm * (0.25 + 0.75 * frac)) if spinning[key] else 0
                write(os.path.join(base, f"fan{n}_input"), rpm)
                duty[(dev["name"], n)] = frac if spinning[key] else 0.0

        cpu_cool = (duty.get(("nct6798", 1), 0) * 0.6 + duty.get(("nct6798", 2), 0) * 0.4)
        cpu_target = 35 + 55 * load - 25 * cpu_cool
        cpu += (cpu_target - cpu) * 0.15
        gpu_target = 32 + 50 * gpu_load - 22 * duty.get(("amdgpu", 1), 0)
        gpu += (gpu_target - gpu) * 0.1
        system = 28 + 0.2 * (cpu - 30)

        write(os.path.join(root, "hwmon0", "temp1_input"), int(system * 1000))
        write(os.path.join(root, "hwmon0", "temp2_input"), int((cpu - 3) * 1000))
        write(os.path.join(root, "hwmon0", "temp3_input"), int((system + 4) * 1000))
        write(os.path.join(root, "hwmon1", "temp1_input"), int(cpu * 1000))
        write(os.path.join(root, "hwmon1", "temp3_input"), int((cpu - 5) * 1000))
        write(os.path.join(root, "hwmon2", "temp1_input"), int(gpu * 1000))
        write(os.path.join(root, "hwmon2", "temp2_input"), int((gpu + 8) * 1000))
        time.sleep(0.5)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    create(sys.argv[1])
    try:
        simulate(sys.argv[1])
    except KeyboardInterrupt:
        pass
