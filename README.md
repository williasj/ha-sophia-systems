<img src="https://raw.githubusercontent.com/williasj/ha-sophia-systems/main/images/sophia_logo.png" alt="SOPHIA Logo" width="200"/>

# SOPHIA Systems

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![version](https://img.shields.io/badge/version-1.2.0-blue.svg)](https://github.com/williasj/ha-sophia-systems/releases)
[![HA minimum](https://img.shields.io/badge/HA-2024.4.0+-orange.svg)](https://www.home-assistant.io/)
[![license](https://img.shields.io/badge/license-PolyForm%20NC-green.svg)](LICENSE)

**Hardware telemetry module for the SOPHIA home intelligence platform.**

Monitors your primary compute hardware and exposes all metrics as Home Assistant sensors
so SOPHIA's AI can reason about system health, thermal state, and infrastructure status.

---

## What It Monitors

| Subsystem | Sensors |
|-----------|---------|
| **TrueNAS Scale** | ZFS pool status, disk SMART temperatures, network throughput, system load/uptime |
| **NVIDIA GPU** | Utilization, VRAM used/free, temperature, power draw, fan speed, clock frequencies, performance state |
| **BMC / Redfish** | CPU temps, memory temps, other temps, fans (RPM), voltage rails, drive health |
| **Raspberry Pi** | CPU usage, CPU temperature, memory usage, disk usage, network I/O (via HA system_monitor) |

GPU metrics are collected via **netdata** running on the TrueNAS host (port 19999).
If netdata is not reachable at setup, GPU sensors will auto-appear once the GPU becomes
visible - no reconfiguration needed.

---

## Hardware Architecture

SOPHIA Systems is designed around a specific hardware model but works with any
TrueNAS Scale host:

```
Primary Compute (Brain)     AMD EPYC / TrueNAS Scale host
  Inference Engine          NVIDIA GPU (via netdata)
  Hardware Watchdog         BMC / Redfish (ASRock Rack or similar)
  Storage                   ZFS pools via TrueNAS API

HA Interface Node           Raspberry Pi 5 (or any HA host)
  Role                      Runs Home Assistant, routes automations
  NOT the brain             The EPYC/TrueNAS server is the compute host
```

This distinction is injected into SOPHIA's LLM context so the AI correctly
understands component criticality and impact scope when reporting issues.

---

## Requirements

- [SOPHIA Core](https://github.com/williasj/ha-sophia-core) must be installed first
- TrueNAS Scale 24.10+ (JSON-RPC 2.0 WebSocket API)
- Home Assistant 2024.4.0+

### GPU Monitoring (optional)

Requires **netdata** running on the TrueNAS host with the NVIDIA device allocated
to the netdata container. In TrueNAS Scale, go to Apps -> netdata -> Edit ->
GPU Configuration and assign the NVIDIA device. netdata must be accessible on
port 19999 from your HA instance.

---

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three-dot menu -> **Custom Repositories**
3. Add `https://github.com/williasj/ha-sophia-systems` as type **Integration**
4. Search for **SOPHIA Systems** and install
5. Restart Home Assistant
6. Go to **Settings -> Devices & Services -> Add Integration** and search for SOPHIA Systems

### Manual

Copy `custom_components/sophia_systems/` to your HA `config/custom_components/` directory
and restart Home Assistant.

---

## Configuration

Setup is a three-step wizard:

**Step 1 - TrueNAS**
- TrueNAS URL (e.g. `http://192.168.1.100` or `https://truenas.local`)
- TrueNAS API key (TrueNAS UI -> Credentials -> API Keys -> Add)
- Poll interval (default 30s)

**Step 2 - BMC / Redfish (optional)**
- BMC URL (e.g. `https://192.168.1.101`)
- BMC username and password
- Leave URL blank to skip BMC monitoring

**Step 3 - Raspberry Pi / HA Host (optional)**
- Entity IDs from your `system_monitor` integration
- Defaults work for a standard Pi 5 HA installation

---

## Services

| Service | Description |
|---------|-------------|
| `sophia_systems.refresh` | Immediately poll all hardware sources, bypassing the normal interval |

---

## GPU Monitoring Notes

GPU stats are polled from netdata's `nvidia_smi` plugin. The GPU UUID is
discovered dynamically on first connection - no manual configuration needed.
If the GPU becomes unavailable (model swap, netdata restart), sensors will
return `unavailable` and auto-recover on the next successful poll.

Metrics collected: utilization, VRAM used/free/total, memory bandwidth
utilization, die temperature, power draw, fan speed, graphics/video/SM/memory
clock frequencies, and performance state (P0-P15).

---

## SOPHIA Ecosystem
| Module | Repository | Status |
|--------|------------|--------|
| SOPHIA Core | [ha-sophia-core](https://github.com/williasj/ha-sophia-core) | Released |
| SOPHIA Climate | [ha-sophia-climate](https://github.com/williasj/ha-sophia-climate) | Released |
| SOPHIA Systems | [ha-sophia-systems](https://github.com/williasj/ha-sophia-systems) | Released |
| SOPHIA Presence | [ha-sophia-presence](https://github.com/williasj/ha-sophia-presence) | Released |

---

## Support

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/sophiadev)

Issues and feature requests: [GitHub Issues](https://github.com/williasj/ha-sophia-systems/issues)

---

## License

PolyForm Noncommercial License 1.0.0

Free for personal, educational, and noncommercial use.
Commercial use requires a separate license - contact
[Scott.J.Williams14@gmail.com](mailto:Scott.J.Williams14@gmail.com) or
[@williasj](https://github.com/williasj) on GitHub.

Copyright Scott Williams (Scott.J.Williams14@gmail.com)
