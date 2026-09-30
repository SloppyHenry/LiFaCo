# Lighting: research, decisions and design

Status: first version implemented (plugin host, catalog, effect engine, plugins `wled`, `openrgb`, `virtual`).
Plugin author guide: [plugin-guide.md](https://github.com/SloppyHenry/LiFaCo-plugins/blob/main/docs/plugin-guide.md)
(repository *LiFaCo-plugins*).

## 1. Goal

Control the RGB lighting of mainboards, graphics cards, RAM, keyboards, mice, CPU coolers, AIOs and fans, plus
WLED strips – from LiFaCo, with effects that can follow fan-control data (for example a colour that follows the
CPU temperature). Everything hardware-specific lives in **plugins** that users can search, install from a catalog,
or write and upload themselves.

## 2. Research: what exists on Linux

Sources: OpenRGB SDK documentation and 1.0 release notes, WLED documentation, liquidctl, OpenRazer, libratbag,
ckb-next, plus findings contributed by the project owner (marked *reported*; verify before relying on them).

| Backend | Interface | Covers | Use in LiFaCo |
|---|---|---|---|
| **OpenRGB** | TCP, port 6742, binary SDK protocol (v6 since 1.0, released 2026-09-12; stable 32-bit device IDs); 1.0 adds a systemd background service | over a thousand devices: ASUS Aura (USB and SMBus), Gigabyte RGB Fusion, ASRock Polychrome, Corsair, Razer, Logitech, SteelSeries, GPUs and RAM over SMBus/I²C | plugin `openrgb` (own minimal client, protocol 4, compatible with 0.9 and 1.0) |
| **WLED** | HTTP JSON API (`/json/state`, `/json/eff` …), WebSocket, mDNS `_wled._tcp`; realtime over UDP (DRGB/DNRGB port 21324), DDP (4048), E1.31 (5568), Art-Net | LED strips/matrices on ESP32/ESP8266 | plugin `wled`: JSON for WLED's own effects, DRGB/DNRGB for LiFaCo's effects |
| **liquidctl** | Python library (already a LiFaCo dependency) | NZXT Kraken/Smart Device/HUE 2, Corsair Hydro/Commander Pro/Core, ASUS Ryujin, MSI Coreliquid, Lian Li Uni SL, Gigabyte RGB Fusion 2, Aquacomputer Farbwerk. *Reported:* experimental ASUS Aura USB driver for Z490/Z590/Z690 boards | **built-in provider** (not a plugin): the service already holds these USB devices open for fan control, so lighting shares the device objects and their lock. Each colour channel is one zone set with mode `fixed`, volatile only, at most every 0.5 s per device. Channels are read from the driver (`_color_channels`, module `_COLOR_CHANNELS`) or known single-`led` drivers (Hydro Platinum/690LC); other devices are not listed yet |
| **OpenRazer** | DKMS kernel module + daemon, D-Bus API, Python library (232 devices) | Razer keyboards, mice, mousemats, headsets, accessories | planned plugin `openrazer` over D-Bus |
| **ckb-next** | own daemon | Corsair keyboards/mice | not needed – Corsair works through OpenRGB; no public API found |
| **libratbag / Piper** | D-Bus | gaming mice (Logitech, SteelSeries, Roccat …) | low priority |
| **g810-led** | CLI | Logitech G keyboards | not needed – OpenRGB covers them |
| **Kernel LED class (multicolor)** | sysfs `multi_intensity` | some laptop keyboards | possible small plugin for laptops |
| **asusctl** | own daemon | ASUS ROG laptops only | out of scope |
| SignalRGB, iCUE, Synapse | – | not available on Linux | replaced by: OpenRGB (Corsair, ASUS), OpenRazer (Razer), and LiFaCo's own effect engine (the part SignalRGB adds) |

### ASUS Aura
There is no ASUS SDK for Linux. OpenRGB controls Aura over USB and SMBus (most mainboards, GPUs, RAM); liquidctl has
an experimental Aura USB driver (*reported*: Z490/Z590/Z690). Nothing fundamental is missing.

### MSI Mystic Light – the delicate case (*reported*, to be verified)
* In OpenRGB the code is not removed, only the detection is disabled, because writing certain data bricked the RGB
  controller of some boards. The damage is believed to come from writing to the controller's **flash/persistent
  memory**, not from changing colours.
* Protocol: USB HID feature reports to devices `1462:xxxx` (mostly `1462:7Dxx` or `1462:921b`); one 185-byte packet
  per zone, a 725-byte packet for individual LEDs. Lean Linux projects exist (`msi-mystic-light-x870e`,
  `msi-mystic-light-web`, `mystic-why`).
* Plan: a dedicated plugin `msi-mystic-light` over hidraw with these safeguards –
  1. only boards whose product ID is on a list of **tested** boards,
  2. only volatile commands, **never** the "save to flash" command,
  3. read and back up the current state before the first write so it can be restored,
  4. off by default; enabling shows a clear warning,
  5. the installer/service sets the udev rule per product ID (LiFaCo does this from the plugin's `usb` permission).

## 3. Decisions

1. **Plugins, not built-in drivers.** A small, stable plugin API; every hardware family is a plugin.
2. **Plugins run as separate processes without root.** The service is root, plugins are not (section 5).
3. **Catalog in its own repository** (`LiFaCo-plugins`) with an `index.json` and reproducible ZIPs with SHA-256.
   Plugins can also be installed from a local ZIP (upload) and written by users.
4. **Easy to write:** a plugin is `plugin.toml` + `plugin.py`; one SDK file with only the standard library;
   `lifacoctl plugin new / dev / validate / pack`.
5. **OpenRGB is the base for broad hardware support**, own protocol client instead of a third-party Python library
   (the protocol is small; no dependency, no version lag).
6. **LiFaCo owns the effects** (like SignalRGB's engine): static, breathing, rainbow, temperature gradient are
   rendered by the service and streamed to plugins as frames; devices with only built-in effects run those.
7. **Lighting is isolated from fan control:** own lock, own threads; a hanging plugin cannot delay a fan update.
8. **Never "save to device".** The plugin API has no such call; colours and effects stay volatile.

Plugins can name **helpers** (`[requires] helpers = ["openrgb"]`): built-in, fixed actions of LiFaCo for what a
plugin depends on. The only helper so far installs the distribution package of OpenRGB (apt/dnf/pacman/zypper/xbps/apk,
through `systemd-run` because the service itself runs in a read-only sandbox). Plugins cannot make the service run
commands, and the helper creates no service and changes no boot setting. Checked in containers: Ubuntu 26.04
(installs OpenRGB 0.9), Debian 13 (package missing, clear message with download link).

## 4. Architecture

```
GUI / lifacoctl ──(Unix socket, JSON)──►  fancontrol-linuxd (root)
                                            ├─ Engine (fans)                      ← own lock
                                            └─ LightingManager                    ← own lock, own threads
                                                 ├─ store      plugins, lighting.json
                                                 ├─ catalog    index.json, download, checksum
                                                 ├─ effects    frame renderer (20 fps when animated)
                                                 ├─ udev       rules for approved USB/i2c access
                                                 └─ PluginProcess ×N ──stdin/stdout JSON lines──► plugin.py
                                                       (bootstrap.py: drop root, no_new_privs, rlimits)
```

* `fancontrol_linux/lighting/` – `manifest.py` (plugin.toml), `store.py` (install/remove, safe ZIP extraction, state),
  `catalog.py`, `process.py` + `bootstrap.py` (sandbox), `effects.py`, `udev.py`, `manager.py`, `devtools.py`,
  `cli.py`, `sdk/lifaco_plugin.py` (what plugins import).
* Device model: device → zones → LEDs; optional hardware **modes** (name, colour count, speed, brightness).
  A device can support direct per-LED colours (`set_colors`), hardware modes (`set_mode`) or both.
* Effects per device are stored in `/etc/fancontrol-linux/lighting.json` (global for now; tying lighting to fan
  profiles is a possible later step).
* IPC commands: `plugin_list`, `plugin_catalog`, `plugin_install`, `plugin_install_file`, `plugin_remove`,
  `plugin_enable`, `plugin_settings`, `plugin_restart`, `light_devices`, `light_set`, `light_power`,
  `light_power_all`, `light_settings`, `light_rescan`, `light_identify`. They bypass the fan-control lock.
* Lighting per profile: saving a profile stores the effect of every device in it, loading one applies them
  (profiles without lighting leave the lights alone). A GUI config push never overwrites lighting.
* A temperature effect can follow a temperature sensor or a fan output (`sensor` = `fan:<control id>`, scale in %).
* Resume from suspend is detected (boot time vs monotonic time) and re-applies all colours; optional
  'turn lights off when LiFaCo stops' (`off_on_exit`).
* Robustness: a plugin that exits or fails is marked with its error and restarted with back-off (up to 5 times,
  then "Restart" in the UI); frames for slow plugins are dropped (newest wins) instead of queued.

## 5. Security model

| Threat | Measure |
|---|---|
| Plugin code running as root | `bootstrap.py` drops to user `lifaco-plugins` (fallback `nobody`; refuses to run if neither exists), verifies it cannot regain root, sets `no_new_privs`, limits (`RLIMIT_NPROC`, `NOFILE`, `CORE`). Verified in a container: uid 999, cannot read `/etc/shadow`, write the plugin or config folders, or `setuid(0)`. |
| Network access | Without the `network` permission the process runs in an empty network namespace (`unshare --net`; only `lo`). Needs util-linux and `CAP_SYS_ADMIN`; if unavailable the plugin still runs unprivileged but with network access (logged). |
| Hardware access | Device nodes are reachable only through the groups `lifaco-usb` / `lifaco-i2c`; a plugin joins a group only if it declared the permission **and** the user approved it. udev rules are generated from approved plugins only, IDs validated as `xxxx:xxxx` hex (no injection). |
| Malicious package | ZIP extraction rejects `..`, absolute paths, symlinks, too many files, oversize; manifest validated; catalog packages verified against SHA-256 from the index; catalog must be HTTPS. |
| Silent permission creep | Approval is tied to the exact permission set; an update that asks for more must be approved again. |
| Unreviewed plugins | Installing from a file shows a warning; catalog plugins are reviewed by pull request. |

Honest limits: a plugin with `network` can talk to the internet; a plugin with `usb`/`i2c` can misuse that hardware.
The sandbox does not use seccomp or mount namespaces beyond what the systemd unit (`ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`) already provides. The catalog is trusted as far as the repository is: there is no
signature beyond HTTPS and the checksum in the index.

## 6. Installer

`install.sh` creates the system user `lifaco-plugins` and the groups `lifaco-plugins`, `lifaco-usb`, `lifaco-i2c`
and the folders `/var/lib/fancontrol-linux/plugins` and `plugin-data`; the systemd unit gets the required
`ReadWritePaths`. `uninstall.sh` removes the udev rules; `--purge` also removes the user and groups.
For the AppImage installation the copied tree under `/opt/fancontrol-linux` is made world-readable
(`chmod -R a+rX`): the AppImage can contain owner-only directories, and the unprivileged plugin user must be able to
read the bundled Python.

## 7. What was verified, and how

| Claim | Verified by |
|---|---|
| Plugin host, catalog, install/remove, effects, settings, crash handling, permissions | 38 unit/integration tests (`tests/test_lighting.py`), also on Python 3.10 (Ubuntu 22.04 container) |
| Plugins run without root, cannot read `/etc/shadow`, write plugin/config folders or `setuid(0)`; no network without the permission | container test with a real root daemon (Debian 13 python image, `--cap-add SYS_ADMIN`) |
| Same with the AppImage's bundled Python | extracted AppImage on Debian 12 (older glibc). This found that the AppImage tree must be world-readable; the installer now does that (the installer step itself was not run end-to-end) |
| GUI (search, install, enable, effect cards), CLI | headless GTK run in Xvfb against a real daemon and a local catalog; screenshots inspected |
| `liquidctl` built-in lighting | tests with simulated liquidctl devices (channels, fixed mode without extra options, throttling, errors, shared lock); **no real device tried** |
| `wled` plugin | tests against a simulated WLED controller (HTTP JSON + UDP) – no real WLED device yet |
| `openrgb` plugin | tests against a simulated OpenRGB server (protocol 4 built from the SDK documentation). **Not yet run against a real OpenRGB server.** |
| systemd unit with new `ReadWritePaths`, udev rule generation and reload, `i2c`/`usb` group access to real devices | **not tested** on a real system yet |

## 8. Roadmap

1. **Done:** plugin host, sandbox, catalog, effect engine, CLI, GUI (Settings → LED devices, Light section),
   plugins `wled`, `openrgb`, `virtual`, developer tools and guide.
2. **Verify** the `openrgb` plugin against real OpenRGB 0.9 and 1.0 servers with hardware (section 7). Decide how LiFaCo helps set up the OpenRGB server
   (systemd service with hardware access).
3. liquidctl lighting: built in and tested with simulated devices only; needs testing with real AIOs/hubs, more drivers (Commander Core/Pro, Aquacomputer, RGB Fusion 2) and hardware modes.
4. `openrazer` plugin (D-Bus).
5. `msi-mystic-light` plugin with the safeguards from section 2.
6. WLED: mDNS without avahi-browse, per-segment control.
7. Done: gradient editor, effects that follow fan speed, lighting per profile, resume/off-on-exit, tray entries
   (Lights on/off), overview with device tiles. Open: effects beyond the four built-in ones, per-zone control.
8. Optional catalog signatures.
