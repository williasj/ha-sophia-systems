# -*- coding: utf-8 -*-
"""SOPHIA Systems coordinator - polls TrueNAS, BMC Redfish, and GPU concurrently."""
import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .truenas_client import TrueNASClient
from .redfish_client import RedfishClient
from .gpu_client import GpuClient
from .const import DEFAULT_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class SophiaSystemsCoordinator(DataUpdateCoordinator):

    def __init__(
        self,
        hass: HomeAssistant,
        truenas_client: TrueNASClient,
        redfish_client: Optional[RedfishClient],
        gpu_client: Optional[GpuClient],
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            hass, _LOGGER, name="sophia_systems",
            update_interval=timedelta(seconds=poll_interval),
        )
        self.truenas = truenas_client
        self.redfish = redfish_client
        self.gpu     = gpu_client
        self._prev_iface: Dict[str, Dict] = {}
        self._prev_ts: Optional[float]    = None

    async def _async_update_data(self) -> Dict[str, Any]:
        try:
            # --- TrueNAS polls -----------------------------------------------
            truenas_results = await asyncio.gather(
                self.truenas.get_system_info(),
                self.truenas.get_pools(),
                self.truenas.get_disk_temperatures(),
                self.truenas.get_interfaces(),
                return_exceptions=True,
            )

            def safe(r, default):
                if isinstance(r, BaseException):
                    _LOGGER.debug("Poll error: %s", r)
                    return default
                return r

            system_info = safe(truenas_results[0], {})
            pools       = safe(truenas_results[1], [])
            disk_temps  = safe(truenas_results[2], {})
            interfaces  = safe(truenas_results[3], [])

            # --- BMC Redfish polls -------------------------------------------
            bmc_cpu_temps   = []
            bmc_mem_temps   = []
            bmc_other_temps = []
            bmc_fans        = []
            bmc_voltages    = []
            bmc_system_info = {}
            bmc_drives      = []

            if self.redfish is not None:
                redfish_results = await asyncio.gather(
                    self.redfish.get_thermal(),
                    self.redfish.get_power(),
                    self.redfish.get_system_info(),
                    self.redfish.get_storage(),
                    return_exceptions=True,
                )
                thermal     = safe(redfish_results[0], {})
                power       = safe(redfish_results[1], {})
                bmc_sysinfo = safe(redfish_results[2], {})
                storage     = safe(redfish_results[3], [])

                bmc_cpu_temps   = thermal.get("cpu_temps", [])
                bmc_mem_temps   = thermal.get("mem_temps", [])
                bmc_other_temps = thermal.get("other_temps", [])
                bmc_fans        = thermal.get("fans", [])
                bmc_voltages    = power.get("voltages", [])
                bmc_system_info = bmc_sysinfo
                bmc_drives      = storage if isinstance(storage, list) else []

            # --- GPU poll ----------------------------------------------------
            gpu_stats: Dict[str, Any] = {}
            if self.gpu is not None:
                try:
                    gpu_stats = await self.gpu.get_gpu_stats()
                except Exception as e:
                    _LOGGER.debug("GPU stats poll error: %s", e)

            # --- Network throughput deltas -----------------------------------
            now        = time.monotonic()
            throughput = self._throughput(interfaces, now)

            return {
                "available":        True,
                "system_info":      system_info,
                "pools":            pools,
                "disk_temps":       disk_temps,
                "interfaces":       interfaces,
                "throughput":       throughput,
                "bmc_available":    self.redfish is not None,
                "bmc_cpu_temps":    bmc_cpu_temps,
                "bmc_mem_temps":    bmc_mem_temps,
                "bmc_other_temps":  bmc_other_temps,
                "bmc_fans":         bmc_fans,
                "bmc_voltages":     bmc_voltages,
                "bmc_system_info":  bmc_system_info,
                "bmc_drives":       bmc_drives,
                "gpu_stats":        gpu_stats,
                "gpu_available":    bool(gpu_stats.get("available")),
            }

        except Exception as exc:
            raise UpdateFailed(f"Systems update failed: {exc}") from exc

    def _throughput(self, interfaces: List[Dict], now: float) -> Dict[str, Dict]:
        result: Dict[str, Dict] = {}
        if self._prev_ts is not None:
            elapsed = now - self._prev_ts
            if elapsed > 0:
                for iface in interfaces:
                    n    = iface["name"]
                    prev = self._prev_iface.get(n, {})
                    d_rx = iface["rx_bytes"] - prev.get("rx", 0)
                    d_tx = iface["tx_bytes"] - prev.get("tx", 0)
                    if d_rx >= 0 and d_tx >= 0:
                        result[n] = {
                            "rx_mbps": round(d_rx * 8 / elapsed / 1e6, 2),
                            "tx_mbps": round(d_tx * 8 / elapsed / 1e6, 2),
                        }
        self._prev_ts    = now
        self._prev_iface = {
            i["name"]: {"rx": i["rx_bytes"], "tx": i["tx_bytes"]}
            for i in interfaces
        }
        return result