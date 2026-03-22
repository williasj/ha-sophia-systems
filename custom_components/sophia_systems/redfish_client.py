# -*- coding: utf-8 -*-
"""
Redfish REST client for ROMED8-2T BMC (ASPEED AST2500 / AMI firmware).

Endpoints:
  GET /redfish/v1/Chassis/Self/Thermal          -> temps + fans
  GET /redfish/v1/Chassis/Self/Power            -> voltages (this BMC has no watt meter)
  GET /redfish/v1/Systems/Self                  -> system summary
  GET /redfish/v1/Systems/Self/Storage          -> storage controllers + drive inventory
  GET /redfish/v1/Systems/Self/Storage/{id}     -> per-controller drive list
"""
import logging
from typing import Any, Dict, List, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)


class RedfishClient:

    def __init__(self, url: str, username: str, password: str, verify_ssl: bool = False) -> None:
        self.base_url    = url.rstrip("/")
        self._username   = username
        self._password   = password
        self._verify_ssl = verify_ssl

    def _auth(self):
        return aiohttp.BasicAuth(self._username, self._password)

    def _connector(self):
        return aiohttp.TCPConnector(ssl=self._verify_ssl)

    async def _get(self, path: str, timeout: int = 15) -> Any:
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession(
            connector=self._connector(),
            auth=self._auth(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                return await r.json(content_type=None)

    async def test_connection(self) -> bool:
        try:
            await self._get("/redfish/v1/", timeout=10)
            return True
        except Exception as e:
            _LOGGER.error("BMC Redfish connection test failed: %s", e)
            return False

    async def get_thermal(self) -> Dict[str, Any]:
        """Return categorised temperatures and fans.
        Only includes sensors where Status.State == Enabled and Reading exists.
        """
        try:
            data = await self._get("/redfish/v1/Chassis/Self/Thermal")
        except Exception as e:
            _LOGGER.debug("Redfish Thermal error: %s", e)
            return {"cpu_temps": [], "mem_temps": [], "other_temps": [], "fans": []}

        _CPU_KW = ("cpu", "processor", "tctl", "tdie", "p0", "p1", "socket")
        _MEM_KW = ("ddr", "dimm", "mem", "ram")

        cpu_temps   = []
        mem_temps   = []
        other_temps = []

        for t in data.get("Temperatures", []):
            status = t.get("Status", {})
            if status.get("State", "") != "Enabled":
                continue
            reading = t.get("ReadingCelsius")
            if reading is None:
                continue
            name = t.get("Name", "Unknown")
            entry = {
                "name":       name,
                "value":      round(float(reading), 1),
                "unit":       "C",
                "health":     status.get("Health", "OK"),
                "upper_crit": t.get("UpperThresholdCritical"),
                "upper_warn": t.get("UpperThresholdNonCritical"),
            }
            name_lc = name.lower()
            if any(k in name_lc for k in _MEM_KW):
                mem_temps.append(entry)
            elif any(k in name_lc for k in _CPU_KW):
                cpu_temps.append(entry)
            else:
                other_temps.append(entry)

        fans = []
        for f in data.get("Fans", []):
            status = f.get("Status", {})
            if status.get("State", "") != "Enabled":
                continue
            rpm = f.get("Reading")
            if rpm is None:
                continue
            fans.append({
                "name":        f.get("Name", "Fan"),
                "value":       int(rpm),
                "unit":        "RPM",
                "health":      status.get("Health", "OK"),
                "lower_warn":  f.get("LowerThresholdNonCritical"),
            })

        return {
            "cpu_temps":   cpu_temps,
            "mem_temps":   mem_temps,
            "other_temps": other_temps,
            "fans":        fans,
        }

    async def get_power(self) -> Dict[str, Any]:
        """Return voltage rails. This BMC does not report watt consumption.
        Returns voltages list only (PSUs absent on this board).
        """
        try:
            data = await self._get("/redfish/v1/Chassis/Self/Power")
        except Exception as e:
            _LOGGER.debug("Redfish Power error: %s", e)
            return {"voltages": []}

        voltages = []
        for v in data.get("Voltages", []):
            status = v.get("Status", {})
            if status.get("State", "") != "Enabled":
                continue
            reading = v.get("ReadingVolts")
            if reading is None:
                continue
            voltages.append({
                "name":        v.get("Name", "Voltage"),
                "value":       round(float(reading), 3),
                "unit":        "V",
                "health":      status.get("Health", "OK"),
                "upper_crit":  v.get("UpperThresholdCritical"),
                "upper_fatal": v.get("UpperThresholdFatal"),
                "lower_crit":  v.get("LowerThresholdCritical"),
                "lower_fatal": v.get("LowerThresholdFatal"),
            })

        return {"voltages": voltages}

    async def get_system_info(self) -> Dict[str, Any]:
        try:
            data = await self._get("/redfish/v1/Systems/Self")
        except Exception as e:
            _LOGGER.debug("Redfish Systems error: %s", e)
            return {}
        return {
            "model":         data.get("Model"),
            "manufacturer":  data.get("Manufacturer"),
            "health":        data.get("Status", {}).get("Health"),
            "power_state":   data.get("PowerState"),
            "bios_version":  data.get("BiosVersion"),
            "cpu_count":     data.get("ProcessorSummary", {}).get("Count"),
            "total_ram_gib": data.get("MemorySummary", {}).get("TotalSystemMemoryGiB"),
        }

    async def get_storage(self) -> List[Dict[str, Any]]:
        """Return drive inventory and health from BMC storage view.

        Walks /redfish/v1/Systems/Self/Storage -> each controller -> each Drive.
        Returns a flat list of drive dicts:
            {
                "name":         str,    e.g. "Disk Bay 0"
                "slot":         str,    e.g. "Slot 0" or None
                "health":       str,    "OK" | "Warning" | "Critical" | "Unknown"
                "state":        str,    "Enabled" | "Absent" | ...
                "capacity_gb":  float,  drive capacity in GB (None if unknown)
                "protocol":     str,    "SATA" | "SAS" | "NVMe" | None
                "media_type":   str,    "HDD" | "SSD" | None
                "model":        str,    drive model string or None
                "controller":   str,    parent controller name
                "present":      bool,   True if drive is actually installed
            }
        """
        drives = []
        try:
            storage_coll = await self._get("/redfish/v1/Systems/Self/Storage")
            members = storage_coll.get("Members", [])
            if not members:
                _LOGGER.debug("BMC Storage: no storage controllers found")
                return []
        except Exception as e:
            _LOGGER.debug("Redfish Storage collection error: %s", e)
            return []

        for member in members:
            ctrl_href = member.get("@odata.id", "")
            if not ctrl_href:
                continue
            try:
                ctrl = await self._get(ctrl_href)
                ctrl_name = ctrl.get("Name", ctrl_href.split("/")[-1])

                # Drives may be inline or linked
                drive_links = ctrl.get("Drives", [])
                for dl in drive_links:
                    drive_href = dl.get("@odata.id", "")
                    if not drive_href:
                        continue
                    try:
                        d = await self._get(drive_href)
                        status     = d.get("Status", {})
                        state      = status.get("State", "Unknown")
                        health     = status.get("Health") or ("OK" if state == "Enabled" else "Unknown")
                        cap_bytes  = d.get("CapacityBytes")
                        cap_gb     = round(cap_bytes / 1e9, 1) if cap_bytes else None
                        present    = state not in ("Absent", "UnavailableOffline")
                        drives.append({
                            "name":        d.get("Name", drive_href.split("/")[-1]),
                            "slot":        d.get("PhysicalLocation", {}).get("PartLocation", {}).get("ServiceLabel"),
                            "health":      health,
                            "state":       state,
                            "capacity_gb": cap_gb,
                            "protocol":    d.get("Protocol"),
                            "media_type":  d.get("MediaType"),
                            "model":       d.get("Model"),
                            "controller":  ctrl_name,
                            "present":     present,
                        })
                    except Exception as e:
                        _LOGGER.debug("Redfish drive %s error: %s", drive_href, e)

            except Exception as e:
                _LOGGER.debug("Redfish controller %s error: %s", ctrl_href, e)

        _LOGGER.debug("BMC Storage: found %d drive entries", len(drives))
        return drives