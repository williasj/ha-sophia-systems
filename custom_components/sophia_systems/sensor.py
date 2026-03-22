# -*- coding: utf-8 -*-
"""SOPHIA Systems sensors."""
import logging
import re
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfTemperature, REVOLUTIONS_PER_MINUTE, UnitOfElectricPotential,
    UnitOfPower, PERCENTAGE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SophiaSystemsCoordinator
from .const import (
    DOMAIN,
    HC_POOL, HC_DISK_TEMP, HC_NETWORK, HC_SYSTEM,
    HC_BMC_CPU_TEMP, HC_BMC_FAN, HC_BMC_OTHER, HC_BMC_POWER, HC_BMC_DRIVE,
    HC_GPU, HC_GPU_TEMP,
    HC_PI_CPU, HC_PI_TEMP, HC_PI_MEM, HC_PI_DISK, HC_PI_NETWORK,
    CONF_PI_ENABLED,
    CONF_PI_CPU_ENTITY, CONF_PI_TEMP_ENTITY, CONF_PI_MEM_ENTITY,
    CONF_PI_DISK_ENTITY, CONF_PI_NET_IN_ENTITY, CONF_PI_NET_OUT_ENTITY,
    DEFAULT_PI_CPU_ENTITY, DEFAULT_PI_TEMP_ENTITY, DEFAULT_PI_MEM_ENTITY,
    DEFAULT_PI_DISK_ENTITY, DEFAULT_PI_NET_IN_ENTITY, DEFAULT_PI_NET_OUT_ENTITY,
    CONF_GPU_ENABLED, DEFAULT_GPU_ENABLED,
)

_LOGGER = logging.getLogger(__name__)

_EPYC_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, "epyc_truenas")},
    name="SOPHIA Systems - EPYC / TrueNAS Scale",
    manufacturer="AMD / iXsystems",
    model="EPYC 7452 / TrueNAS Scale",
)

_BMC_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, "romed8_bmc")},
    name="SOPHIA Systems - ROMED8-2T BMC",
    manufacturer="ASRock Rack / ASPEED",
    model="ROMED8-2T BMC",
)

_GPU_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, "rtx5090")},
    name="SOPHIA Systems - RTX 5090",
    manufacturer="NVIDIA",
    model="GeForce RTX 5090",
    via_device=(DOMAIN, "epyc_truenas"),  # child of EPYC device
)

# Pi is the HA interface node - explicitly NOT described as the host/brain
_PI_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, "raspberry_pi5")},
    name="SOPHIA Systems - Raspberry Pi 5 (HA Interface)",
    manufacturer="Raspberry Pi Foundation",
    model="Pi 5 - HA Interface Node",
)

# Health category constants not in const.py (kept here to match __init__.py)
HC_BMC_VOLTAGE  = "bmc_voltage"
HC_BMC_MEM_TEMP = "bmc_memory_temperature"


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    coordinator: SophiaSystemsCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    data = coordinator.data or {}
    entities: List[SensorEntity] = []

    # TrueNAS
    entities.append(SophiaTrueNASSystemSensor(coordinator, entry))
    for pool in data.get("pools", []):
        entities.append(SophiaPoolStatusSensor(coordinator, entry, pool["name"]))
    for disk_name in data.get("disk_temps", {}).keys():
        entities.append(SophiaDiskTempSensor(coordinator, entry, disk_name))
    for iface in data.get("interfaces", []):
        entities.append(SophiaNetworkSensor(coordinator, entry, iface["name"]))

    # BMC Redfish
    if coordinator.redfish is not None:
        entities.append(SophiaBmcPsuSensor(coordinator, entry))
        for s in data.get("bmc_cpu_temps", []):
            entities.append(SophiaBmcTempSensor(coordinator, entry, s["name"], HC_BMC_CPU_TEMP, "bmc_cpu_temps"))
        for s in data.get("bmc_mem_temps", []):
            entities.append(SophiaBmcTempSensor(coordinator, entry, s["name"], HC_BMC_MEM_TEMP, "bmc_mem_temps"))
        for s in data.get("bmc_other_temps", []):
            entities.append(SophiaBmcTempSensor(coordinator, entry, s["name"], HC_BMC_OTHER, "bmc_other_temps"))
        for s in data.get("bmc_fans", []):
            entities.append(SophiaBmcFanSensor(coordinator, entry, s["name"]))
        for v in data.get("bmc_voltages", []):
            entities.append(SophiaBmcVoltageSensor(coordinator, entry, v["name"]))
        # BMC drive health sensors (one per present drive)
        for drive in data.get("bmc_drives", []):
            if drive.get("present", True):
                entities.append(SophiaBmcDriveSensor(coordinator, entry, drive["name"]))

    # GPU
    if entry.data.get(CONF_GPU_ENABLED, DEFAULT_GPU_ENABLED) and coordinator.gpu is not None:
        if data.get("gpu_available"):
            entities.append(SophiaGpuSensor(coordinator, entry))
            entities.append(SophiaGpuTemperatureSensor(coordinator, entry))
            entities.append(SophiaGpuPowerSensor(coordinator, entry))
            entities.append(SophiaGpuVramUsedSensor(coordinator, entry))
            entities.append(SophiaGpuVramFreeSensor(coordinator, entry))
            entities.append(SophiaGpuMemUtilSensor(coordinator, entry))
            entities.append(SophiaGpuFanSensor(coordinator, entry))
            entities.append(SophiaGpuClockSensor(coordinator, entry))
            entities.append(SophiaGpuPerfStateSensor(coordinator, entry))

    # Pi mirrors (HA interface node)
    if entry.data.get(CONF_PI_ENABLED, True):
        pi_map = {
            HC_PI_CPU:     entry.data.get(CONF_PI_CPU_ENTITY,     DEFAULT_PI_CPU_ENTITY),
            HC_PI_TEMP:    entry.data.get(CONF_PI_TEMP_ENTITY,    DEFAULT_PI_TEMP_ENTITY),
            HC_PI_MEM:     entry.data.get(CONF_PI_MEM_ENTITY,     DEFAULT_PI_MEM_ENTITY),
            HC_PI_DISK:    entry.data.get(CONF_PI_DISK_ENTITY,    DEFAULT_PI_DISK_ENTITY),
            HC_PI_NETWORK: entry.data.get(CONF_PI_NET_IN_ENTITY,  DEFAULT_PI_NET_IN_ENTITY),
        }
        for hc, src in pi_map.items():
            entities.append(SophiaPiMirrorSensor(hass, entry, hc, src.strip()))

    async_add_entities(entities, update_before_add=True)
    hass.data[DOMAIN][entry.entry_id]["entities"] = entities


# ---------------------------------------------------------------------------
# BASE CLASSES
# ---------------------------------------------------------------------------

class _TrueNASBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_info = _EPYC_DEVICE

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def _data(self):
        return self.coordinator.data or {}


class _BmcBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_info = _BMC_DEVICE

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def _data(self):
        return self.coordinator.data or {}


class _GpuBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_info = _GPU_DEVICE

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry

    @property
    def _data(self):
        return self.coordinator.data or {}

    @property
    def _gpu(self) -> Dict[str, Any]:
        return self._data.get("gpu_stats", {})


# ---------------------------------------------------------------------------
# TRUENAS SENSORS
# ---------------------------------------------------------------------------

class SophiaTrueNASSystemSensor(_TrueNASBase):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id   = f"{DOMAIN}_truenas_system"
        self._attr_name        = "TrueNAS System"
        self._attr_icon        = "mdi:server"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        info = self._data.get("system_info", {})
        la   = info.get("loadavg", [])
        return round(la[0], 2) if la else None

    @property
    def extra_state_attributes(self):
        info   = self._data.get("system_info", {})
        la     = info.get("loadavg", [0, 0, 0])
        up     = int(info.get("uptime_seconds", 0))
        h, rem = divmod(up, 3600)
        return {
            "sophia_health_category": HC_SYSTEM,
            "hostname":    info.get("hostname"),
            "version":     info.get("version"),
            "uptime":      f"{h}h {rem // 60}m",
            "loadavg_1m":  round(la[0], 2) if len(la) > 0 else None,
            "loadavg_5m":  round(la[1], 2) if len(la) > 1 else None,
            "loadavg_15m": round(la[2], 2) if len(la) > 2 else None,
            "ram_gb":      info.get("physmem_gb"),
        }


class SophiaPoolStatusSensor(_TrueNASBase):
    def __init__(self, coordinator, entry, pool_name):
        super().__init__(coordinator, entry)
        self._pool_name      = pool_name
        self._attr_unique_id = f"{DOMAIN}_pool_{_slug(pool_name)}"
        self._attr_name      = f"Pool {pool_name}"
        self._attr_icon      = "mdi:database"

    def _pool(self):
        return next((p for p in self._data.get("pools", []) if p["name"] == self._pool_name), None)

    @property
    def native_value(self):
        p = self._pool()
        return p.get("status", "UNKNOWN") if p else "unavailable"

    @property
    def icon(self):
        p = self._pool()
        return {"ONLINE": "mdi:database-check", "DEGRADED": "mdi:database-alert",
                "FAULTED": "mdi:database-remove"}.get(
                    p.get("status", "") if p else "", "mdi:database")

    @property
    def extra_state_attributes(self):
        p = self._pool() or {}
        return {"sophia_health_category": HC_POOL, "pool_name": self._pool_name,
                "healthy": p.get("healthy"), "size_tb": p.get("size_tb"),
                "free_tb": p.get("free_tb"), "allocated_pct": p.get("allocated_pct")}


class SophiaDiskTempSensor(_TrueNASBase):
    def __init__(self, coordinator, entry, disk_name):
        super().__init__(coordinator, entry)
        self._disk_name                        = disk_name
        self._attr_unique_id                   = f"{DOMAIN}_disk_temp_{_slug(disk_name)}"
        self._attr_name                        = f"Disk {disk_name} Temp"
        self._attr_icon                        = "mdi:harddisk"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        t = self._data.get("disk_temps", {}).get(self._disk_name)
        return round(float(t), 1) if t is not None else None

    @property
    def extra_state_attributes(self):
        return {"sophia_health_category": HC_DISK_TEMP, "disk_name": self._disk_name}


class SophiaNetworkSensor(_TrueNASBase):
    def __init__(self, coordinator, entry, iface_name):
        super().__init__(coordinator, entry)
        self._iface                = iface_name
        self._attr_unique_id       = f"{DOMAIN}_net_{_slug(iface_name)}"
        self._attr_name            = f"Net {iface_name}"
        self._attr_icon            = "mdi:ethernet"
        self._attr_native_unit_of_measurement = "Mbit/s"
        self._attr_state_class     = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        tp = self._data.get("throughput", {}).get(self._iface, {})
        return round(tp.get("rx_mbps", 0) + tp.get("tx_mbps", 0), 2) if tp else None

    @property
    def extra_state_attributes(self):
        tp   = self._data.get("throughput", {}).get(self._iface, {})
        info = next((i for i in self._data.get("interfaces", []) if i["name"] == self._iface), {})
        return {"sophia_health_category": HC_NETWORK, "interface": self._iface,
                "rx_mbps": tp.get("rx_mbps", 0), "tx_mbps": tp.get("tx_mbps", 0),
                "link_state": info.get("link_state", "UNKNOWN"), "speed_mbps": info.get("speed_mbps")}


# ---------------------------------------------------------------------------
# BMC SENSORS
# ---------------------------------------------------------------------------

class SophiaBmcTempSensor(_BmcBase):
    def __init__(self, coordinator, entry, sensor_name, health_category, bucket):
        super().__init__(coordinator, entry)
        self._sensor_name      = sensor_name
        self._hc               = health_category
        self._bucket           = bucket
        self._attr_unique_id   = f"{DOMAIN}_bmc_temp_{_slug(sensor_name)}"
        self._attr_name        = sensor_name
        self._attr_icon        = "mdi:thermometer"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    def _find(self):
        return next((s for s in self._data.get(self._bucket, []) if s["name"] == self._sensor_name), None)

    @property
    def native_value(self):
        s = self._find()
        return round(float(s["value"]), 1) if s else None

    @property
    def extra_state_attributes(self):
        s = self._find() or {}
        return {"sophia_health_category": self._hc, "sensor_name": self._sensor_name,
                "health": s.get("health"), "upper_crit": s.get("upper_crit"),
                "upper_warn": s.get("upper_warn")}


class SophiaBmcFanSensor(_BmcBase):
    def __init__(self, coordinator, entry, fan_name):
        super().__init__(coordinator, entry)
        self._fan_name         = fan_name
        self._attr_unique_id   = f"{DOMAIN}_bmc_fan_{_slug(fan_name)}"
        self._attr_name        = fan_name
        self._attr_icon        = "mdi:fan"
        self._attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
        self._attr_state_class = SensorStateClass.MEASUREMENT

    def _find(self):
        return next((s for s in self._data.get("bmc_fans", []) if s["name"] == self._fan_name), None)

    @property
    def native_value(self):
        s = self._find()
        return int(s["value"]) if s else None

    @property
    def extra_state_attributes(self):
        s = self._find() or {}
        return {"sophia_health_category": HC_BMC_FAN, "fan_name": self._fan_name,
                "health": s.get("health"), "lower_warn": s.get("lower_warn")}


class SophiaBmcPsuSensor(_BmcBase):
    """Static sensor - this BMC's PSU doesn't expose power draw via Redfish."""
    _attr_should_poll = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_bmc_psu_status"
        self._attr_name      = "PSU Status"
        self._attr_icon      = "mdi:power-plug-off"

    @property
    def native_value(self) -> str:
        return "PSU does not support usage statistics"

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": "bmc_psu",
            "note": "Installed PSU does not expose power consumption or health data via Redfish. Voltage rail monitoring is available separately.",
        }


class SophiaBmcVoltageSensor(_BmcBase):
    def __init__(self, coordinator, entry, voltage_name):
        super().__init__(coordinator, entry)
        self._voltage_name     = voltage_name
        self._attr_unique_id   = f"{DOMAIN}_bmc_voltage_{_slug(voltage_name)}"
        self._attr_name        = voltage_name
        self._attr_icon        = "mdi:flash"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class  = SensorDeviceClass.VOLTAGE
        self._attr_state_class   = SensorStateClass.MEASUREMENT

    def _find(self):
        return next((v for v in self._data.get("bmc_voltages", []) if v["name"] == self._voltage_name), None)

    @property
    def native_value(self):
        v = self._find()
        return round(float(v["value"]), 3) if v else None

    @property
    def extra_state_attributes(self):
        v = self._find() or {}
        return {"sophia_health_category": HC_BMC_VOLTAGE, "rail_name": self._voltage_name,
                "health": v.get("health"), "upper_crit": v.get("upper_crit"),
                "lower_crit": v.get("lower_crit")}


class SophiaBmcDriveSensor(_BmcBase):
    """Drive health as seen by the BMC via Redfish Storage.

    State is the health string (OK / Warning / Critical / Unknown).
    Attributes carry capacity, protocol, media type, and model.
    """

    def __init__(self, coordinator, entry, drive_name: str):
        super().__init__(coordinator, entry)
        self._drive_name     = drive_name
        self._attr_unique_id = f"{DOMAIN}_bmc_drive_{_slug(drive_name)}"
        self._attr_name      = f"Drive {drive_name}"
        self._attr_icon      = "mdi:harddisk"

    def _find(self) -> Dict[str, Any]:
        return next(
            (d for d in self._data.get("bmc_drives", []) if d["name"] == self._drive_name),
            {},
        )

    @property
    def native_value(self) -> Optional[str]:
        d = self._find()
        return d.get("health", "Unknown") if d else "unavailable"

    @property
    def icon(self) -> str:
        d    = self._find()
        h    = d.get("health", "") if d else ""
        mt   = (d.get("media_type") or "").upper() if d else ""
        base = "mdi:harddisk" if mt != "SSD" else "mdi:harddisk"
        return {"OK": "mdi:harddisk", "Warning": "mdi:harddisk-remove",
                "Critical": "mdi:harddisk-remove"}.get(h, "mdi:harddisk")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        d = self._find() or {}
        return {
            "sophia_health_category": HC_BMC_DRIVE,
            "drive_name":   self._drive_name,
            "slot":         d.get("slot"),
            "state":        d.get("state"),
            "capacity_gb":  d.get("capacity_gb"),
            "protocol":     d.get("protocol"),
            "media_type":   d.get("media_type"),
            "model":        d.get("model"),
            "controller":   d.get("controller"),
            "present":      d.get("present", True),
        }


# ---------------------------------------------------------------------------
# GPU SENSOR
# ---------------------------------------------------------------------------

class SophiaGpuSensor(_GpuBase):
    """GPU utilization with VRAM, temperature, and power draw as attributes.

    State = GPU core utilization % (the most glanceable metric).
    All other nvidia-smi stats live in attributes.
    """

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                   = f"{DOMAIN}_gpu_utilization"
        self._attr_name                        = "GPU Utilization"
        self._attr_icon                        = "mdi:expansion-card"
        self._attr_native_unit_of_measurement  = PERCENTAGE
        self._attr_state_class                 = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        util = self._gpu.get("utilization_pct")
        return round(float(util), 1) if util is not None else None

    @property
    def icon(self) -> str:
        util = self._gpu.get("utilization_pct", 0) or 0
        if util >= 90:
            return "mdi:expansion-card"
        if util >= 50:
            return "mdi:expansion-card"
        return "mdi:expansion-card-variant"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        g = self._gpu
        used  = g.get("mem_used_mib")
        total = g.get("mem_total_mib")
        vram_pct = round(used / total * 100, 1) if used and total else None
        return {
            "sophia_health_category": HC_GPU,
            "vram_used_mib":   used,
            "vram_total_mib":  total,
            "vram_pct":        vram_pct,
            "temperature_c":   g.get("temperature_c"),
            "power_w":         g.get("power_w"),
            "source":          "netdata/nvidia_smi",
        }


class SophiaGpuTemperatureSensor(_GpuBase):
    """GPU die temperature in Celsius."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_temperature"
        self._attr_name                       = "GPU Temperature"
        self._attr_icon                       = "mdi:thermometer"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("temperature_c")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": HC_GPU_TEMP,
            "uuid": self._gpu.get("uuid"),
            "performance_state": self._gpu.get("performance_state"),
        }


class SophiaGpuPowerSensor(_GpuBase):
    """GPU power draw in Watts."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_power"
        self._attr_name                       = "GPU Power Draw"
        self._attr_icon                       = "mdi:flash"
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class               = SensorDeviceClass.POWER
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("power_w")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": HC_GPU,
            "uuid": self._gpu.get("uuid"),
        }


class SophiaGpuVramUsedSensor(_GpuBase):
    """VRAM used in MiB."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_vram_used"
        self._attr_name                       = "GPU VRAM Used"
        self._attr_icon                       = "mdi:memory"
        self._attr_native_unit_of_measurement = "MiB"
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("mem_used_mib")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        g = self._gpu
        used  = g.get("mem_used_mib")
        total = g.get("mem_total_mib")
        return {
            "sophia_health_category": HC_GPU,
            "vram_total_mib": total,
            "vram_pct": round(used / total * 100, 1) if used and total else None,
            "uuid": g.get("uuid"),
        }


class SophiaGpuVramFreeSensor(_GpuBase):
    """VRAM free in MiB."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_vram_free"
        self._attr_name                       = "GPU VRAM Free"
        self._attr_icon                       = "mdi:memory"
        self._attr_native_unit_of_measurement = "MiB"
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("mem_free_mib")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": HC_GPU,
            "vram_total_mib": self._gpu.get("mem_total_mib"),
            "uuid": self._gpu.get("uuid"),
        }


class SophiaGpuMemUtilSensor(_GpuBase):
    """GPU memory bandwidth utilization percent."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_mem_util"
        self._attr_name                       = "GPU Memory Utilization"
        self._attr_icon                       = "mdi:memory"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("mem_util_pct")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": HC_GPU,
            "uuid": self._gpu.get("uuid"),
        }


class SophiaGpuFanSensor(_GpuBase):
    """GPU fan speed percent."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_fan"
        self._attr_name                       = "GPU Fan Speed"
        self._attr_icon                       = "mdi:fan"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("fan_speed_pct")
        return round(float(v), 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": HC_GPU,
            "uuid": self._gpu.get("uuid"),
        }


class SophiaGpuClockSensor(_GpuBase):
    """GPU graphics core clock in MHz."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id                  = f"{DOMAIN}_gpu_clock"
        self._attr_name                       = "GPU Graphics Clock"
        self._attr_icon                       = "mdi:speedometer"
        self._attr_native_unit_of_measurement = "MHz"
        self._attr_state_class                = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        v = self._gpu.get("clock_graphics_mhz")
        return round(float(v), 0) if v is not None else None

    @property
    def extra_state_attributes(self):
        g = self._gpu
        return {
            "sophia_health_category": HC_GPU,
            "clock_mem_mhz":     g.get("clock_mem_mhz"),
            "clock_sm_mhz":      g.get("clock_sm_mhz"),
            "clock_video_mhz":   g.get("clock_video_mhz"),
            "uuid":              g.get("uuid"),
        }


class SophiaGpuPerfStateSensor(_GpuBase):
    """GPU performance state (0=P0 max perf, 15=P15 deep idle)."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id  = f"{DOMAIN}_gpu_perf_state"
        self._attr_name       = "GPU Performance State"
        self._attr_icon       = "mdi:gauge"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[int]:
        v = self._gpu.get("performance_state")
        return int(v) if v is not None else None

    @property
    def extra_state_attributes(self):
        ps = self._gpu.get("performance_state")
        label = f"P{ps}" if ps is not None else None
        return {
            "sophia_health_category": HC_GPU,
            "state_label": label,
            "description": "P0=maximum performance, P8=idle, P12-P15=deep idle",
            "uuid": self._gpu.get("uuid"),
        }


# ---------------------------------------------------------------------------
# PI MIRROR SENSORS  (HA interface node - not the host)
# ---------------------------------------------------------------------------

_HC_TO_NAME = {
    HC_PI_CPU:     "Pi CPU Usage",
    HC_PI_TEMP:    "Pi CPU Temperature",
    HC_PI_MEM:     "Pi Memory Usage",
    HC_PI_DISK:    "Pi Disk Usage",
    HC_PI_NETWORK: "Pi Network In",
}

_HC_TO_ICON = {
    HC_PI_CPU:     "mdi:cpu-64-bit",
    HC_PI_TEMP:    "mdi:thermometer",
    HC_PI_MEM:     "mdi:memory",
    HC_PI_DISK:    "mdi:sd",
    HC_PI_NETWORK: "mdi:ethernet",
}


class SophiaPiMirrorSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_info     = _PI_DEVICE
    _attr_should_poll     = True

    def __init__(self, hass, entry, health_category, source_entity_id):
        self.hass            = hass
        self._entry          = entry
        self._hc             = health_category
        self._source         = source_entity_id.strip()
        self._state          = None
        self._unit           = None
        self._source_raw     = None
        self._attr_unique_id = f"{DOMAIN}_pi_{_slug(health_category)}"
        self._attr_name      = _HC_TO_NAME.get(health_category, health_category)
        self._attr_icon      = _HC_TO_ICON.get(health_category, "mdi:raspberry-pi")

    async def async_added_to_hass(self):
        self._read_source()

        @callback
        def _on_change(event):
            ns = event.data.get("new_state")
            if ns is not None:
                self._apply_state(ns)
                self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._source], _on_change)
        )

    def _apply_state(self, state):
        if state is None:
            self._source_raw = "entity not found"
            self._state = None
            return
        self._source_raw = state.state
        if state.state in ("unknown", "unavailable", ""):
            self._state = None
            return
        try:
            self._state = round(float(state.state), 1)
        except (ValueError, TypeError):
            self._state = state.state
        self._unit = state.attributes.get("unit_of_measurement")

    def _read_source(self):
        self._apply_state(self.hass.states.get(self._source))

    async def async_update(self):
        self._read_source()

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def extra_state_attributes(self):
        return {
            "sophia_health_category": self._hc,
            "node_role":        "ha_interface",
            "node_description": "HA interface and sensor collection node - not the primary compute host",
            "source_entity":    self._source,
            "source_raw_state": self._source_raw,
        }