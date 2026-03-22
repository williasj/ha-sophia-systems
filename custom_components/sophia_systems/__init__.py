# -*- coding: utf-8 -*-
"""SOPHIA Systems - hardware health integration."""
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN, VERSION,
    CONF_TRUENAS_URL, CONF_TRUENAS_API_KEY, CONF_TRUENAS_VERIFY_SSL,
    CONF_BMC_URL, CONF_BMC_USER, CONF_BMC_PASSWORD, CONF_BMC_VERIFY_SSL,
    CONF_POLL_INTERVAL, CONF_PI_ENABLED,
    CONF_PI_CPU_ENTITY, CONF_PI_TEMP_ENTITY, CONF_PI_MEM_ENTITY,
    CONF_PI_DISK_ENTITY, CONF_PI_NET_IN_ENTITY, CONF_PI_NET_OUT_ENTITY,
    CONF_GPU_ENABLED, CONF_GPU_NETDATA_URL,
    DEFAULT_POLL_INTERVAL, DEFAULT_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL,
    DEFAULT_PI_CPU_ENTITY, DEFAULT_PI_TEMP_ENTITY, DEFAULT_PI_MEM_ENTITY,
    DEFAULT_PI_DISK_ENTITY, DEFAULT_PI_NET_IN_ENTITY, DEFAULT_PI_NET_OUT_ENTITY,
    DEFAULT_GPU_ENABLED,
    HC_POOL, HC_DISK_TEMP, HC_NETWORK, HC_SYSTEM, HC_GPU, HC_GPU_TEMP, HC_BMC_DRIVE,
    HC_BMC_CPU_TEMP, HC_BMC_FAN, HC_BMC_OTHER, HC_BMC_POWER,
    HC_PI_CPU, HC_PI_TEMP, HC_PI_MEM, HC_PI_DISK, HC_PI_NETWORK,
    COMPONENT_ROLES, LLM_HARDWARE_CONTEXT,
)
from .coordinator import SophiaSystemsCoordinator
from .truenas_client import TrueNASClient
from .redfish_client import RedfishClient
from .gpu_client import GpuClient, derive_netdata_url
from .sensor import _slug

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]

HC_BMC_VOLTAGE  = "bmc_voltage"
HC_BMC_PSU      = "bmc_psu"
HC_BMC_MEM_TEMP = "bmc_memory_temperature"


def _lookup(hass: HomeAssistant, unique_id: str) -> Optional[str]:
    """Return actual entity_id for a unique_id, or None."""
    ent_reg = er.async_get(hass)
    return ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)


def _lookups(hass: HomeAssistant, unique_ids: List[str]) -> List[str]:
    """Return list of resolved entity_ids, skipping missing ones."""
    result = []
    for uid in unique_ids:
        eid = _lookup(hass, uid)
        if eid:
            result.append(eid)
        else:
            _LOGGER.debug("unique_id not in registry: %s", uid)
    return result


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if "sophia_core" not in hass.data:
        _LOGGER.error("SOPHIA Systems requires SOPHIA Core.")
        return False

    registry = hass.data["sophia_core"]["registry"]

    truenas_client = TrueNASClient(
        url=entry.data.get(CONF_TRUENAS_URL, ""),
        api_key=entry.data.get(CONF_TRUENAS_API_KEY, ""),
        verify_ssl=entry.data.get(CONF_TRUENAS_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )

    redfish_client: Optional[RedfishClient] = None
    bmc_url  = entry.data.get(CONF_BMC_URL, "").strip()
    bmc_user = entry.data.get(CONF_BMC_USER, "").strip()
    bmc_pwd  = entry.data.get(CONF_BMC_PASSWORD, "").strip()
    if bmc_url and bmc_user and bmc_pwd:
        redfish_client = RedfishClient(
            url=bmc_url, username=bmc_user, password=bmc_pwd,
            verify_ssl=entry.data.get(CONF_BMC_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL),
        )
        _LOGGER.info("BMC Redfish configured: %s", bmc_url)

    # GPU client - derive netdata URL from TrueNAS host unless overridden
    gpu_client: Optional[GpuClient] = None
    if entry.data.get(CONF_GPU_ENABLED, DEFAULT_GPU_ENABLED):
        truenas_url   = entry.data.get(CONF_TRUENAS_URL, "")
        netdata_url   = entry.data.get(CONF_GPU_NETDATA_URL, "").strip()
        if not netdata_url and truenas_url:
            netdata_url = derive_netdata_url(truenas_url)
        if netdata_url:
            gpu_client = GpuClient(netdata_url)
            reachable  = await gpu_client.test_connection()
            if reachable:
                _LOGGER.info("GPU monitoring enabled via netdata: %s", netdata_url)
            else:
                _LOGGER.info(
                    "GPU client configured (%s) but netdata not reachable or no nvidia_smi charts. "
                    "GPU sensors will appear once the GPU is visible to TrueNAS host.",
                    netdata_url,
                )

    coordinator = SophiaSystemsCoordinator(
        hass, truenas_client, redfish_client, gpu_client,
        poll_interval=entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator, "entities": []}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    capabilities = _build_capabilities(hass, entry, coordinator)
    if registry.register_module(DOMAIN, capabilities):
        _LOGGER.info("SOPHIA Systems registered with Core (%d sensors)", len(capabilities.get("sensors", [])))
    else:
        _LOGGER.error("SOPHIA Systems failed to register with Core")

    async def handle_refresh(call):
        await coordinator.async_refresh()

    hass.services.async_register(DOMAIN, "refresh", handle_refresh)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if "sophia_core" in hass.data:
        hass.data["sophia_core"]["registry"].unregister_module(DOMAIN)
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        coordinator = entry_data.get("coordinator")
        if coordinator is not None and coordinator.truenas is not None:
            await coordinator.truenas.close()
    return ok


def _build_capabilities(hass, entry, coordinator) -> Dict[str, Any]:
    """Build capabilities using real entity IDs from entity registry."""
    data       = coordinator.data or {}
    pi_enabled = entry.data.get(CONF_PI_ENABLED, True)
    gpu_data   = data.get("gpu_stats", {})

    def uid(s): return f"{DOMAIN}_{s}"

    system_uid     = uid("truenas_system")
    pool_uids      = [uid(f"pool_{_slug(p['name'])}") for p in data.get("pools", [])]
    disk_uids      = [uid(f"disk_temp_{_slug(d)}") for d in data.get("disk_temps", {})]
    net_uids       = [uid(f"net_{_slug(i['name'])}") for i in data.get("interfaces", [])]
    bmc_cpu_uids   = [uid(f"bmc_temp_{_slug(s['name'])}") for s in data.get("bmc_cpu_temps", [])]
    bmc_mem_uids   = [uid(f"bmc_temp_{_slug(s['name'])}") for s in data.get("bmc_mem_temps", [])]
    bmc_other_uids = [uid(f"bmc_temp_{_slug(s['name'])}") for s in data.get("bmc_other_temps", [])]
    bmc_fan_uids   = [uid(f"bmc_fan_{_slug(s['name'])}") for s in data.get("bmc_fans", [])]
    bmc_volt_uids  = [uid(f"bmc_voltage_{_slug(v['name'])}") for v in data.get("bmc_voltages", [])]
    bmc_psu_uid    = uid("bmc_psu_status")
    bmc_drive_uids = [
        uid(f"bmc_drive_{_slug(d['name'])}")
        for d in data.get("bmc_drives", [])
        if d.get("present", True)
    ]

    # GPU sensors (only if available)
    gpu_uid  = uid("gpu_utilization") if gpu_data.get("available") else None
    gpu_uids = (
        [
            uid("gpu_utilization"),
            uid("gpu_temperature"),
            uid("gpu_power"),
            uid("gpu_vram_used"),
            uid("gpu_vram_free"),
            uid("gpu_mem_util"),
            uid("gpu_fan"),
            uid("gpu_clock"),
            uid("gpu_perf_state"),
        ]
        if gpu_data.get("available") else []
    )

    pi_uids = {}
    if pi_enabled:
        pi_uids = {
            HC_PI_CPU:     uid(f"pi_{_slug(HC_PI_CPU)}"),
            HC_PI_TEMP:    uid(f"pi_{_slug(HC_PI_TEMP)}"),
            HC_PI_MEM:     uid(f"pi_{_slug(HC_PI_MEM)}"),
            HC_PI_DISK:    uid(f"pi_{_slug(HC_PI_DISK)}"),
            HC_PI_NETWORK: uid(f"pi_{_slug(HC_PI_NETWORK)}"),
        }

    system_eid     = _lookup(hass, system_uid)
    pool_eids      = _lookups(hass, pool_uids)
    disk_eids      = _lookups(hass, disk_uids)
    net_eids       = _lookups(hass, net_uids)
    bmc_cpu_eids   = _lookups(hass, bmc_cpu_uids)
    bmc_mem_eids   = _lookups(hass, bmc_mem_uids)
    bmc_other_eids = _lookups(hass, bmc_other_uids)
    bmc_fan_eids   = _lookups(hass, bmc_fan_uids)
    bmc_volt_eids  = _lookups(hass, bmc_volt_uids)
    bmc_psu_eid    = _lookup(hass, bmc_psu_uid)
    bmc_drive_eids = _lookups(hass, bmc_drive_uids)
    gpu_eid        = _lookup(hass, gpu_uid) if gpu_uid else None
    gpu_eids       = _lookups(hass, gpu_uids)

    pi_eids = {}
    for hc, uid_str in pi_uids.items():
        eid = _lookup(hass, uid_str)
        if eid:
            pi_eids[hc] = eid

    all_sensors = (
        ([system_eid] if system_eid else [])
        + pool_eids + disk_eids + net_eids
        + bmc_cpu_eids + bmc_mem_eids + bmc_other_eids
        + bmc_fan_eids + bmc_volt_eids
        + ([bmc_psu_eid] if bmc_psu_eid else [])
        + bmc_drive_eids
        + gpu_eids
        + list(pi_eids.values())
    )

    return {
        "name":         "SOPHIA Systems",
        "version":      VERSION,
        "requires_llm": False,
        "services":     ["sophia_systems.refresh"],
        "sensors":      all_sensors,
        # ------------------------------------------------------------------
        # LLM context: injected into AI system prompts so SOPHIA correctly
        # understands hardware hierarchy and component criticality.
        # ------------------------------------------------------------------
        "llm_hardware_context": LLM_HARDWARE_CONTEXT,
        "component_roles":      COMPONENT_ROLES,
        "metadata": {
            "description":    "Hardware health - EPYC/TrueNAS + BMC + GPU + Pi interface",
            "truenas_url":    entry.data.get(CONF_TRUENAS_URL),
            "bmc_url":        entry.data.get(CONF_BMC_URL),
            "bmc_available":  coordinator.redfish is not None,
            "gpu_available":  bool(gpu_data.get("available")),
            "pool_count":     len(pool_eids),
            "disk_count":     len(disk_eids),
            "bmc_fan_count":  len(bmc_fan_eids),
            "bmc_drive_count": len(bmc_drive_eids),
        },
        "health_entities": {
            "truenas": {
                HC_SYSTEM:    [system_eid] if system_eid else [],
                HC_POOL:      pool_eids,
                HC_DISK_TEMP: disk_eids,
                HC_NETWORK:   net_eids,
            },
            "bmc": {
                HC_BMC_CPU_TEMP:  bmc_cpu_eids,
                HC_BMC_MEM_TEMP:  bmc_mem_eids,
                HC_BMC_OTHER:     bmc_other_eids,
                HC_BMC_FAN:       bmc_fan_eids,
                HC_BMC_VOLTAGE:   bmc_volt_eids,
                HC_BMC_PSU:       [bmc_psu_eid] if bmc_psu_eid else [],
                HC_BMC_DRIVE:     bmc_drive_eids,
            },
            "gpu": {
                HC_GPU:      gpu_eids,
                HC_GPU_TEMP: [_lookup(hass, uid("gpu_temperature"))] if gpu_data.get("available") else [],
            },
            "pi": pi_eids,
        },
        "dashboard_config": _build_dashboard(
            entry, data,
            system_eid, pool_eids, disk_eids, net_eids,
            bmc_cpu_eids, bmc_mem_eids, bmc_other_eids,
            bmc_fan_eids, bmc_volt_eids, bmc_psu_eid, bmc_drive_eids,
            gpu_eid, gpu_eids, gpu_data,
            pi_eids, pi_enabled,
        ),
    }


def _build_dashboard(
    entry, data,
    system_eid, pool_eids, disk_eids, net_eids,
    bmc_cpu_eids, bmc_mem_eids, bmc_other_eids,
    bmc_fan_eids, bmc_volt_eids, bmc_psu_eid, bmc_drive_eids,
    gpu_eid, gpu_eids, gpu_data,
    pi_eids, pi_enabled,
) -> Dict[str, Any]:
    """Build Systems dashboard tab with real entity IDs."""

    def elist(eids):
        return [{"entity": e} for e in eids]

    fb_eid = system_eid or "sensor.sophia_systems_truenas_system"
    fb = lambda msg: [{"entity": fb_eid, "name": msg}]

    cards = [
        {
            "type": "markdown",
            "content": (
                "# SOPHIA Systems Health\n"
                "**Primary Compute**: AMD EPYC 7452 / TrueNAS Scale  |  "
                "**Inference Engine**: RTX 5090  |  "
                "**HA Interface Node**: Raspberry Pi 5\n\n"
                "> The Pi is the HA interface, not SOPHIA's host. The EPYC server is the primary compute platform."
            ),
        },
    ]

    # GPU card - shown first if available (it's the most critical for AI ops)
    if gpu_eid:
        vram_total = gpu_data.get("mem_total_mib")
        vram_label = f"{round(vram_total / 1024, 0):.0f} GB VRAM" if vram_total else "VRAM"
        cards.append({
            "type": "entities",
            "title": f"RTX 5090 - Inference Engine ({vram_label})",
            "show_header_toggle": False,
            "entities": [{"entity": e} for e in gpu_eids] if gpu_eids else [{"entity": gpu_eid}],
        })
    else:
        cards.append({
            "type": "markdown",
            "content": (
                "**GPU monitoring**: Not yet available. "
                "Requires netdata (port 19999) on the TrueNAS host with NVIDIA drivers loaded. "
                "SOPHIA will auto-detect when the GPU becomes visible."
            ),
        })

    cards += [
        {
            "type": "entities", "title": "TrueNAS System (Primary Compute)", "show_header_toggle": False,
            "entities": elist([system_eid]) if system_eid else fb("TrueNAS unavailable"),
        },
        {
            "type": "entities", "title": "Storage Pools", "show_header_toggle": False,
            "entities": elist(pool_eids) or fb("No pools found"),
        },
        {
            "type": "entities", "title": "Disk Temperatures (SMART)", "show_header_toggle": False,
            "entities": elist(disk_eids) or fb("No disk temps found"),
        },
        {
            "type": "entities", "title": "Network Interfaces", "show_header_toggle": False,
            "entities": elist(net_eids) or fb("No interfaces found"),
        },
    ]

    bmc_configured = bool(entry.data.get(CONF_BMC_URL))
    if bmc_configured:
        cards += [
            {
                "type": "entities", "title": "BMC - CPU Temperature", "show_header_toggle": False,
                "entities": elist(bmc_cpu_eids) or fb("No CPU temps from BMC"),
            },
            {
                "type": "entities", "title": "BMC - Memory Temperatures", "show_header_toggle": False,
                "entities": elist(bmc_mem_eids) or fb("No memory temps from BMC"),
            },
            {
                "type": "entities", "title": "BMC - Other Temperatures", "show_header_toggle": False,
                "entities": elist(bmc_other_eids) or fb("No other temps from BMC"),
            },
            {
                "type": "entities", "title": "BMC - Fans", "show_header_toggle": False,
                "entities": elist(bmc_fan_eids) or fb("No fans from BMC"),
            },
            {
                "type": "entities", "title": "BMC - Voltage Rails", "show_header_toggle": False,
                "entities": elist(bmc_volt_eids) or fb("No voltage data"),
            },
            {
                "type": "entities", "title": "BMC - PSU", "show_header_toggle": False,
                "entities": elist([bmc_psu_eid]) if bmc_psu_eid else fb("PSU sensor unavailable"),
            },
        ]
        if bmc_drive_eids:
            cards.append({
                "type": "entities", "title": "BMC - Drive Health", "show_header_toggle": False,
                "entities": elist(bmc_drive_eids),
            })
        else:
            cards.append({
                "type": "markdown",
                "content": (
                    "**BMC Drive Health**: No drives found via Redfish Storage. "
                    "This is normal if the ROMED8-2T BMC does not enumerate drives "
                    "through its storage controller path. SMART temps above cover drive health."
                ),
            })
    else:
        cards.append({
            "type": "markdown",
            "content": "**BMC not configured.** Add BMC credentials in SOPHIA Systems -> Configure.",
        })

    if pi_enabled and pi_eids:
        cards.append({
            "type": "entities",
            "title": "Raspberry Pi 5 - HA Interface Node",
            "show_header_toggle": False,
            "entities": [{"entity": e} for e in pi_eids.values()],
        })

    return {"title": "Systems", "path": "systems", "icon": "mdi:server", "badges": [], "cards": cards}