# -*- coding: utf-8 -*-
"""
SOPHIA Systems - NVIDIA GPU stats via TrueNAS Scale / Fangtooth netdata.

Netdata v2 on TrueNAS Scale 25.x uses UUID-suffixed chart names:
  nvidia_smi.gpu_{uuid}_gpu_utilization
  nvidia_smi.gpu_{uuid}_temperature
  nvidia_smi.gpu_{uuid}_power_draw
  nvidia_smi.gpu_{uuid}_frame_buffer_memory_usage  (dims: free, used, reserved - in bytes)
  nvidia_smi.gpu_{uuid}_memory_utilization
  nvidia_smi.gpu_{uuid}_fan_speed_perc
  nvidia_smi.gpu_{uuid}_clock_freq                 (dims: graphics, video, sm, mem)
  nvidia_smi.gpu_{uuid}_performance_state

The GPU UUID is discovered dynamically from /api/v1/charts on first connection
and cached for the lifetime of the client instance. If netdata is unreachable
or no nvidia_smi charts exist, every method returns an empty dict - no errors
surface to HA.
"""
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Bytes to MiB
_B_TO_MIB = 1 / 1_048_576


def derive_netdata_url(truenas_url: str) -> str:
    """Derive netdata base URL from TrueNAS URL (same host, port 19999)."""
    parsed = urlparse(truenas_url)
    host = parsed.hostname or truenas_url.split("/")[0]
    return f"http://{host}:19999"


class GpuClient:
    """Real-time NVIDIA GPU stats from netdata running on TrueNAS Scale / Fangtooth."""

    def __init__(self, netdata_base_url: str) -> None:
        self._base = netdata_base_url.rstrip("/")
        self._available: Optional[bool] = None   # None = untested
        self._gpu_prefix: Optional[str] = None   # e.g. "nvidia_smi.gpu_gpu-{uuid}"
        self._gpu_uuid: Optional[str] = None     # raw UUID string

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _chart(self, suffix: str) -> Optional[str]:
        """Build full chart name from cached prefix + suffix."""
        if self._gpu_prefix is None:
            return None
        return f"{self._gpu_prefix}_{suffix}"

    async def _discover_gpu_prefix(self) -> Optional[str]:
        """Query /api/v1/charts and find the nvidia_smi GPU chart prefix.

        Netdata v2 names charts like:
            nvidia_smi.gpu_gpu-{uuid}_temperature

        We locate any chart starting with 'nvidia_smi.gpu_' and ending with
        '_temperature' (most reliable anchor) to extract the prefix.
        Returns the prefix string or None if not found.
        """
        try:
            url = f"{self._base}/api/v1/charts"
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as s:
                async with s.get(url) as r:
                    r.raise_for_status()
                    data = await r.json()

            charts: Dict[str, Any] = data.get("charts", {})

            # Look for a chart matching the temperature anchor
            for name in charts:
                if name.startswith("nvidia_smi.gpu_") and name.endswith("_temperature"):
                    prefix = name[: -len("_temperature")]
                    # Extract UUID for logging
                    parts = prefix.split("nvidia_smi.gpu_", 1)
                    uuid = parts[1] if len(parts) > 1 else "unknown"
                    _LOGGER.info(
                        "GPU client: discovered chart prefix '%s' (UUID: %s) at %s",
                        prefix, uuid, self._base,
                    )
                    self._gpu_uuid = uuid
                    return prefix

            # Fallback: any nvidia_smi.gpu_ chart to at least confirm GPU presence
            for name in charts:
                if name.startswith("nvidia_smi.gpu_"):
                    _LOGGER.warning(
                        "GPU client: found nvidia_smi charts at %s but could not "
                        "extract prefix from '%s'", self._base, name,
                    )
                    return None

            _LOGGER.info(
                "GPU client: netdata reachable at %s but no nvidia_smi.gpu_ charts found "
                "(GPU device may not be allocated to netdata container)", self._base,
            )
            return None

        except Exception as exc:
            _LOGGER.debug("GPU chart discovery failed (%s): %s", self._base, exc)
            return None

    async def _fetch_single(self, chart: str, timeout: int = 8) -> Optional[float]:
        """Fetch the latest value from dimension index [1] of a chart.

        Returns None on 404, connection error, or missing data.
        """
        if chart is None:
            return None
        url = f"{self._base}/api/v1/data?chart={chart}&points=1&format=json&group=last"
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as s:
                async with s.get(url) as r:
                    if r.status == 404:
                        return None
                    r.raise_for_status()
                    d = await r.json()
                    rows = d.get("data", [])
                    if rows and len(rows[0]) > 1:
                        val = rows[0][1]
                        return float(val) if val is not None else None
        except aiohttp.ClientResponseError:
            return None
        except Exception as exc:
            _LOGGER.debug("Netdata chart %s fetch error: %s", chart, exc)
            return None
        return None

    async def _fetch_multi(self, chart: str, timeout: int = 8) -> Optional[List[Optional[float]]]:
        """Fetch the latest row from a multi-dimension chart.

        Returns list of values [dim1, dim2, ...] (index 0 is timestamp, excluded).
        Returns None on error or missing data.
        """
        if chart is None:
            return None
        url = f"{self._base}/api/v1/data?chart={chart}&points=1&format=json&group=last"
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as s:
                async with s.get(url) as r:
                    if r.status == 404:
                        return None
                    r.raise_for_status()
                    d = await r.json()
                    rows = d.get("data", [])
                    if rows and len(rows[0]) > 1:
                        # row = [timestamp, dim1, dim2, ...]
                        row = rows[0]
                        return [
                            float(v) if v is not None else None
                            for v in row[1:]
                        ]
        except aiohttp.ClientResponseError:
            return None
        except Exception as exc:
            _LOGGER.debug("Netdata multi-chart %s fetch error: %s", chart, exc)
            return None
        return None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def test_connection(self) -> bool:
        """Check whether netdata is reachable and nvidia_smi charts exist.

        Also populates the cached GPU chart prefix for subsequent polls.
        """
        prefix = await self._discover_gpu_prefix()
        if prefix is not None:
            self._gpu_prefix = prefix
            self._available = True
            return True
        self._available = False
        return False

    async def get_gpu_stats(self) -> Dict[str, Any]:
        """Return GPU stats dict. Empty dict if GPU unavailable or netdata unreachable.

        Compatible with sensor.py expectations:
            {
                "available":        True,
                "utilization_pct":  float,   # 0-100  GPU core %
                "mem_used_mib":     float,   # VRAM used
                "mem_free_mib":     float,   # VRAM free
                "mem_total_mib":    float,   # derived: used + free
                "mem_util_pct":     float,   # VRAM bandwidth utilisation %
                "temperature_c":    float,   # die temp Celsius
                "power_w":          float,   # power draw W
                "fan_speed_pct":    float,   # fan duty %
                "clock_graphics_mhz": float,
                "clock_mem_mhz":    float,
                "clock_sm_mhz":     float,
                "performance_state": int,    # 0 = P0 (max perf) to 15 = P15
                "uuid":             str,
            }
        """
        # Lazy prefix discovery (handles first call before test_connection)
        if self._gpu_prefix is None:
            prefix = await self._discover_gpu_prefix()
            if prefix is None:
                return {}
            self._gpu_prefix = prefix

        results: Dict[str, Any] = {}

        # --- Scalar charts ---------------------------------------------------
        scalar_map = {
            "utilization_pct": self._chart("gpu_utilization"),
            "mem_util_pct":    self._chart("memory_utilization"),
            "temperature_c":   self._chart("temperature"),
            "power_w":         self._chart("power_draw"),
            "fan_speed_pct":   self._chart("fan_speed_perc"),
        }

        for key, chart in scalar_map.items():
            val = await self._fetch_single(chart)
            if val is not None:
                results[key] = round(val, 1)

        # --- Frame buffer memory (bytes to MiB) --------------------------------
        # Dimensions: [free, used, reserved]
        mem_vals = await self._fetch_multi(self._chart("frame_buffer_memory_usage"))
        if mem_vals is not None and len(mem_vals) >= 2:
            free_b = mem_vals[0]
            used_b = mem_vals[1]
            if free_b is not None:
                results["mem_free_mib"] = round(free_b * _B_TO_MIB, 1)
            if used_b is not None:
                results["mem_used_mib"] = round(used_b * _B_TO_MIB, 1)
            if free_b is not None and used_b is not None:
                results["mem_total_mib"] = round((free_b + used_b) * _B_TO_MIB, 0)

        # --- Clock frequencies -----------------------------------------------
        # Dimensions: [graphics, video, sm, mem]
        clk_vals = await self._fetch_multi(self._chart("clock_freq"))
        if clk_vals is not None and len(clk_vals) >= 4:
            dim_names = ["clock_graphics_mhz", "clock_video_mhz", "clock_sm_mhz", "clock_mem_mhz"]
            for i, dim_key in enumerate(dim_names):
                if clk_vals[i] is not None:
                    results[dim_key] = round(clk_vals[i], 0)

        # --- Performance state (P0-P15, find which is =1) --------------------
        ps_vals = await self._fetch_multi(self._chart("performance_state"))
        if ps_vals is not None:
            for i, v in enumerate(ps_vals):
                if v is not None and v >= 0.5:
                    results["performance_state"] = i
                    break

        if not results:
            return {}

        results["available"] = True
        if self._gpu_uuid:
            results["uuid"] = self._gpu_uuid

        return results

    async def get_gpu_inventory(self) -> Dict[str, Any]:
        """Return GPU name/driver info from netdata info endpoint."""
        try:
            url = f"{self._base}/api/v1/info"
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as s:
                async with s.get(url) as r:
                    r.raise_for_status()
                    data = await r.json()
                    modules = data.get("modules", {})
                    gpu_module = modules.get("nvidia_smi", {})
                    return {
                        "netdata_version": data.get("version"),
                        "gpu_plugin_info": gpu_module,
                        "gpu_uuid":        self._gpu_uuid,
                        "chart_prefix":    self._gpu_prefix,
                    }
        except Exception as exc:
            _LOGGER.debug("GPU inventory fetch error: %s", exc)
            return {}