import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fancontrol_linux import naming  # noqa: E402

PCI_IDS = """# test database
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t7480  Navi 33 [Radeon RX 7600/7600 XT/7600M XT/7600S/7700S / PRO W7600]
10de  NVIDIA Corporation
\t2206  GA102 [GeForce RTX 3080]
10ec  Realtek Semiconductor Co., Ltd.
\t8125  RTL8125 2.5GbE Controller
\t\t1458 e000  Onboard Ethernet
"""


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        with open(os.path.join(d, "pci.ids"), "w") as f:
            f.write(PCI_IDS)
        with open(os.path.join(d, "cpuinfo"), "w") as f:
            f.write("processor\t: 0\nmodel name\t: AMD Ryzen 9 5950X 16-Core Processor\n")
        os.environ["FANCONTROL_PCI_IDS"] = os.path.join(d, "pci.ids")
        os.environ["FANCONTROL_CPUINFO"] = os.path.join(d, "cpuinfo")
        naming._pci_cache.clear()
        naming._cpu_cache.clear()

    def tearDown(self):
        del os.environ["FANCONTROL_PCI_IDS"], os.environ["FANCONTROL_CPUINFO"]
        naming._pci_cache.clear()
        naming._cpu_cache.clear()
        self.tmp.cleanup()

    def _hwmon_with_pci(self, vendor, device, nested=True):
        pci = os.path.join(self.tmp.name, "devices", "0000:04:00.0")
        target = os.path.join(pci, "mdio_bus", "phy") if nested else pci
        os.makedirs(target, exist_ok=True)
        for name, value in (("vendor", vendor), ("device", device), ("class", "0x020000")):
            with open(os.path.join(pci, name), "w") as f:
                f.write(value + "\n")
        hw = os.path.join(self.tmp.name, "hwmon9")
        os.makedirs(hw, exist_ok=True)
        os.symlink(target, os.path.join(hw, "device"))
        return hw

    def test_devices(self):
        self.assertEqual(naming.device_name("/nonexistent", "k10temp"), "AMD Ryzen 9 5950X")
        self.assertEqual(naming.device_name("/nonexistent", "it8689_61030507"), "Mainboard (ITE IT8689)")
        self.assertEqual(naming.device_name("/nonexistent", "nct6798"), "Mainboard (Nuvoton NCT6798)")
        self.assertEqual(naming.device_name("/nonexistent", "gigabyte_wmi"), "Mainboard (Gigabyte WMI)")
        self.assertEqual(naming.device_name("/nonexistent", "acpitz"), "ACPI thermal zone")
        self.assertEqual(naming.device_name("/nonexistent", "somechip"), "somechip")

    def test_pci_names(self):
        self.assertEqual(naming.pci_name("0x1002", "0x7480"), "AMD Radeon RX 7600")
        self.assertEqual(naming.pci_name("0x10de", "0x2206"), "NVIDIA GeForce RTX 3080")
        self.assertEqual(naming.pci_name("0x10ec", "0x8125"), "Realtek RTL8125 2.5GbE Controller")
        self.assertIsNone(naming.pci_name("0x10ec", "0xffff"))
        hw = self._hwmon_with_pci("0x10ec", "0x8125")
        self.assertEqual(naming.device_name(hw, "r8169_0_400:00"), "Realtek RTL8125 2.5GbE Controller")

    def test_nvme_model(self):
        dev = os.path.join(self.tmp.name, "nvme0")
        os.makedirs(dev)
        with open(os.path.join(dev, "model"), "w") as f:
            f.write("KINGSTON SNV2S1000G       \n")
        hw = os.path.join(self.tmp.name, "hwmon2")
        os.makedirs(hw)
        os.symlink(dev, os.path.join(hw, "device"))
        self.assertEqual(naming.device_name(hw, "nvme"), "NVMe KINGSTON SNV2S1000G")

    def test_channels(self):
        self.assertEqual(naming.channel_label("k10temp", "temp", 1, "Tctl"), "Package (Tctl)")
        self.assertEqual(naming.channel_label("k10temp", "temp", 3, "Tccd1"), "CCD 1")
        self.assertEqual(naming.channel_label("nct6798", "temp", 3, "AUXTIN0"), "Auxiliary 1")
        self.assertEqual(naming.channel_label("amdgpu", "temp", 2, "junction"), "Hotspot")
        self.assertEqual(naming.channel_label("it8689", "fan", 2, None), "Fan 2")
        self.assertEqual(naming.channel_label("it8689", "pwm", 1, None), "Fan header 1")
        self.assertEqual(naming.channel_label("nvme", "temp", 1, None), "Temperature")
        self.assertEqual(naming.channel_label("nvme", "temp", 1, "Composite"), "Composite")


if __name__ == "__main__":
    unittest.main()
