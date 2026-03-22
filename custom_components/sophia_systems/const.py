# -*- coding: utf-8 -*-
"""Constants for SOPHIA Systems."""

DOMAIN = "sophia_systems"
VERSION = "1.2.0"

# TrueNAS
CONF_TRUENAS_URL        = "truenas_url"
CONF_TRUENAS_API_KEY    = "truenas_api_key"
CONF_TRUENAS_VERIFY_SSL = "truenas_verify_ssl"
CONF_POLL_INTERVAL      = "poll_interval"

# BMC / Redfish
CONF_BMC_URL            = "bmc_url"
CONF_BMC_USER           = "bmc_user"
CONF_BMC_PASSWORD       = "bmc_password"
CONF_BMC_VERIFY_SSL     = "bmc_verify_ssl"

# GPU (netdata on TrueNAS Scale host, port 19999)
CONF_GPU_ENABLED        = "gpu_enabled"
CONF_GPU_NETDATA_URL    = "gpu_netdata_url"  # optional override; auto-derived if empty

# Pi
CONF_PI_ENABLED         = "pi_enabled"
CONF_PI_CPU_ENTITY      = "pi_cpu_entity"
CONF_PI_TEMP_ENTITY     = "pi_temp_entity"
CONF_PI_MEM_ENTITY      = "pi_mem_entity"
CONF_PI_DISK_ENTITY     = "pi_disk_entity"
CONF_PI_NET_IN_ENTITY   = "pi_net_in_entity"
CONF_PI_NET_OUT_ENTITY  = "pi_net_out_entity"

# Defaults
DEFAULT_POLL_INTERVAL   = 30
DEFAULT_VERIFY_SSL      = True
DEFAULT_BMC_USER        = "admin"
DEFAULT_BMC_VERIFY_SSL  = False  # self-signed certs common on BMCs
DEFAULT_GPU_ENABLED     = True

DEFAULT_PI_CPU_ENTITY     = "sensor.system_monitor_processor_use"
DEFAULT_PI_TEMP_ENTITY    = "sensor.system_monitor_processor_temperature"
DEFAULT_PI_MEM_ENTITY     = "sensor.system_monitor_memory_usage"
DEFAULT_PI_DISK_ENTITY    = "sensor.system_monitor_disk_usage"
DEFAULT_PI_NET_IN_ENTITY  = "sensor.system_monitor_network_in_end0"
DEFAULT_PI_NET_OUT_ENTITY = "sensor.system_monitor_network_out_end0"

# Health categories
HC_POOL         = "pool_status"
HC_CPU_TEMP     = "cpu_temperature"
HC_GPU          = "gpu_utilization"
HC_GPU_TEMP     = "gpu_temperature"
HC_MEM_TEMP     = "memory_temperature"
HC_DISK_TEMP    = "disk_temperature"
HC_FAN          = "fan_speed"
HC_OTHER_TEMP   = "other_temperature"
HC_NETWORK      = "network_throughput"
HC_SYSTEM       = "system_info"
HC_BMC_CPU_TEMP = "bmc_cpu_temperature"
HC_BMC_FAN      = "bmc_fan_speed"
HC_BMC_OTHER    = "bmc_other"
HC_BMC_POWER    = "bmc_power"
HC_BMC_DRIVE    = "bmc_drive_health"
HC_PI_CPU       = "pi_cpu"
HC_PI_TEMP      = "pi_temperature"
HC_PI_MEM       = "pi_memory"
HC_PI_DISK      = "pi_disk"
HC_PI_NETWORK   = "pi_network"

# ---- Component role metadata ------------------------------------------------
# Injected into SOPHIA Core's LLM context so the AI correctly understands
# the hardware hierarchy. The Pi is NOT the host - it is a sensor/interface node.
# ---------------------------------------------------------------------------

COMPONENT_ROLES = {
    "primary_compute": {
        "label":       "Primary Compute (Brain)",
        "hardware":    "AMD EPYC 7452 / TrueNAS Scale",
        "role":        "SOPHIA's primary compute platform. Runs LLM inference, heavy processing, ZFS storage. This is the real host.",
        "criticality": "critical",
    },
    "gpu": {
        "label":       "GPU / Inference Engine",
        "hardware":    "NVIDIA RTX 5090",
        "role":        "LLM inference engine. Currently running the primary language model. High-priority hardware - temperature and utilization directly affect AI response quality.",
        "criticality": "critical",
    },
    "bmc": {
        "label":       "BMC (Hardware Watchdog)",
        "hardware":    "ASRock Rack ROMED8-2T / ASPEED AST2500",
        "role":        "Out-of-band hardware monitor for the EPYC server. Watches CPU temps, memory temps, fans, and voltage rails independently of the OS.",
        "criticality": "high",
    },
    "storage": {
        "label":       "Storage (ZFS Pools)",
        "hardware":    "TrueNAS Scale ZFS",
        "role":        "Long-term data persistence. Stores SOPHIA's memory, automation history, and home data. Pool health directly affects data integrity.",
        "criticality": "high",
    },
    "ha_interface": {
        "label":       "HA Interface Node (NOT the host/brain)",
        "hardware":    "Raspberry Pi 5",
        "role":        "Home Assistant interface and sensor collection node. Routes automations, talks to smart home devices, and serves the HA UI. It is NOT SOPHIA's brain - it is a lightweight edge node. The EPYC server is the actual compute host.",
        "criticality": "medium",
    },
}

LLM_HARDWARE_CONTEXT = (
    "SOPHIA's hardware hierarchy (important - do not confuse the interface node with the host):\n"
    "- PRIMARY HOST / BRAIN: AMD EPYC 7452 server running TrueNAS Scale. This is where SOPHIA's compute lives.\n"
    "- INFERENCE ENGINE: NVIDIA RTX 5090 in the EPYC server - runs the LLM. Temperature and power draw are "
    "critical indicators of AI health.\n"
    "- HARDWARE WATCHDOG: ROMED8-2T BMC (ASPEED AST2500) - monitors the EPYC server out-of-band. "
    "Fan, temp, and voltage alerts from BMC indicate hardware stress.\n"
    "- STORAGE: TrueNAS Scale ZFS pools - long-term memory and home data. DEGRADED pool status is urgent.\n"
    "- HA INTERFACE NODE: Raspberry Pi 5 - runs Home Assistant, collects sensor data, executes automations. "
    "It is a lightweight edge device, NOT the host. Pi issues affect the smart home interface but not SOPHIA's core compute.\n"
    "When reporting system health, always distinguish between EPYC/GPU issues (compute impact) vs Pi issues (interface impact)."
)
