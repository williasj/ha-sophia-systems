# -*- coding: utf-8 -*-
"""Config flow for SOPHIA Systems."""
import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_TRUENAS_URL, CONF_TRUENAS_API_KEY, CONF_TRUENAS_VERIFY_SSL,
    CONF_POLL_INTERVAL,
    CONF_BMC_URL, CONF_BMC_USER, CONF_BMC_PASSWORD, CONF_BMC_VERIFY_SSL,
    CONF_PI_ENABLED,
    CONF_PI_CPU_ENTITY, CONF_PI_TEMP_ENTITY, CONF_PI_MEM_ENTITY,
    CONF_PI_DISK_ENTITY, CONF_PI_NET_IN_ENTITY, CONF_PI_NET_OUT_ENTITY,
    DEFAULT_POLL_INTERVAL, DEFAULT_VERIFY_SSL,
    DEFAULT_BMC_USER, DEFAULT_BMC_VERIFY_SSL,
    DEFAULT_PI_CPU_ENTITY, DEFAULT_PI_TEMP_ENTITY, DEFAULT_PI_MEM_ENTITY,
    DEFAULT_PI_DISK_ENTITY, DEFAULT_PI_NET_IN_ENTITY, DEFAULT_PI_NET_OUT_ENTITY,
)
from .truenas_client import TrueNASClient
from .redfish_client import RedfishClient

_LOGGER = logging.getLogger(__name__)


def _strip_strings(d: Dict) -> Dict:
    return {k: v.strip() if isinstance(v, str) else v for k, v in d.items()}


class SophiaSystemsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Three-step config: TrueNAS -> BMC -> Pi."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._config_data: Dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1 - TrueNAS URL and API key."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            user_input = _strip_strings(user_input)
            url = user_input.get(CONF_TRUENAS_URL, "")
            key = user_input.get(CONF_TRUENAS_API_KEY, "")

            if not url or not key:
                errors["base"] = "truenas_required"
            else:
                client = TrueNASClient(
                    url=url, api_key=key,
                    verify_ssl=user_input.get(CONF_TRUENAS_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                )
                if not await client.test_connection():
                    errors[CONF_TRUENAS_URL] = "cannot_connect"
                else:
                    self._config_data = dict(user_input)
                    return await self.async_step_bmc()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_TRUENAS_URL):                                    str,
                vol.Required(CONF_TRUENAS_API_KEY):                                str,
                vol.Optional(CONF_TRUENAS_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL):
                    vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
            }),
            errors=errors,
            description_placeholders={"info": "TrueNAS URL example: http://192.168.1.100\nAPI key: TrueNAS UI -> Credentials -> API Keys -> Add"},
        )

    async def async_step_bmc(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 2 - BMC Redfish credentials (optional)."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            user_input = _strip_strings(user_input)
            url  = user_input.get(CONF_BMC_URL, "")
            user = user_input.get(CONF_BMC_USER, "")
            pwd  = user_input.get(CONF_BMC_PASSWORD, "")

            if url and user and pwd:
                client = RedfishClient(
                    url=url, username=user, password=pwd,
                    verify_ssl=user_input.get(CONF_BMC_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL),
                )
                if not await client.test_connection():
                    errors[CONF_BMC_URL] = "cannot_connect_bmc"
                else:
                    self._config_data.update(user_input)
                    return await self.async_step_pi()
            else:
                # All empty means skip BMC - that is fine
                self._config_data.update(user_input)
                return await self.async_step_pi()

        return self.async_show_form(
            step_id="bmc",
            data_schema=vol.Schema({
                vol.Optional(CONF_BMC_URL,        default=""):                        str,
                vol.Optional(CONF_BMC_USER,       default=DEFAULT_BMC_USER):          str,
                vol.Optional(CONF_BMC_PASSWORD,   default=""):                        str,
                vol.Optional(CONF_BMC_VERIFY_SSL, default=DEFAULT_BMC_VERIFY_SSL):    bool,
            }),
            errors=errors,
            description_placeholders={"info": "BMC URL example: https://192.168.1.101\nLeave URL blank to skip BMC monitoring.\nDefault user is typically admin or ADMIN."},
        )

    async def async_step_pi(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 3 - Pi system_monitor entity IDs."""
        if user_input is not None:
            self._config_data.update(_strip_strings(user_input))
            return self.async_create_entry(title="SOPHIA Systems", data=self._config_data)

        return self.async_show_form(
            step_id="pi",
            data_schema=vol.Schema({
                vol.Optional(CONF_PI_ENABLED,        default=True):                        bool,
                vol.Optional(CONF_PI_CPU_ENTITY,     default=DEFAULT_PI_CPU_ENTITY):       str,
                vol.Optional(CONF_PI_TEMP_ENTITY,    default=DEFAULT_PI_TEMP_ENTITY):      str,
                vol.Optional(CONF_PI_MEM_ENTITY,     default=DEFAULT_PI_MEM_ENTITY):       str,
                vol.Optional(CONF_PI_DISK_ENTITY,    default=DEFAULT_PI_DISK_ENTITY):      str,
                vol.Optional(CONF_PI_NET_IN_ENTITY,  default=DEFAULT_PI_NET_IN_ENTITY):    str,
                vol.Optional(CONF_PI_NET_OUT_ENTITY, default=DEFAULT_PI_NET_OUT_ENTITY):   str,
            }),
            description_placeholders={"info": "Enter entity IDs from your system_monitor integration.\nCheck Developer Tools -> States for exact IDs."},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SophiaSystemsOptionsFlow()


class SophiaSystemsOptionsFlow(config_entries.OptionsFlow):
    """Options flow."""

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        errors: Dict[str, str] = {}
        current = self.config_entry.data

        if user_input is not None:
            user_input = _strip_strings(user_input)

            url = user_input.get(CONF_TRUENAS_URL, "")
            key = user_input.get(CONF_TRUENAS_API_KEY, "")
            if url and key:
                client = TrueNASClient(url=url, api_key=key,
                    verify_ssl=user_input.get(CONF_TRUENAS_VERIFY_SSL, DEFAULT_VERIFY_SSL))
                if not await client.test_connection():
                    errors[CONF_TRUENAS_URL] = "cannot_connect"

            bmc_url = user_input.get(CONF_BMC_URL, "")
            bmc_usr = user_input.get(CONF_BMC_USER, "")
            bmc_pwd = user_input.get(CONF_BMC_PASSWORD, "")
            if bmc_url and bmc_usr and bmc_pwd and not errors:
                bmc = RedfishClient(url=bmc_url, username=bmc_usr, password=bmc_pwd,
                    verify_ssl=user_input.get(CONF_BMC_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL))
                if not await bmc.test_connection():
                    errors[CONF_BMC_URL] = "cannot_connect_bmc"

            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**current, **user_input}
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_TRUENAS_URL,        default=current.get(CONF_TRUENAS_URL,        "")):                   str,
                vol.Optional(CONF_TRUENAS_API_KEY,    default=current.get(CONF_TRUENAS_API_KEY,    "")):                   str,
                vol.Optional(CONF_TRUENAS_VERIFY_SSL, default=current.get(CONF_TRUENAS_VERIFY_SSL, DEFAULT_VERIFY_SSL)):   bool,
                vol.Optional(CONF_POLL_INTERVAL,      default=current.get(CONF_POLL_INTERVAL,      DEFAULT_POLL_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                vol.Optional(CONF_BMC_URL,        default=current.get(CONF_BMC_URL,        "")):                           str,
                vol.Optional(CONF_BMC_USER,       default=current.get(CONF_BMC_USER,       DEFAULT_BMC_USER)):             str,
                vol.Optional(CONF_BMC_PASSWORD,   default=current.get(CONF_BMC_PASSWORD,   "")):                           str,
                vol.Optional(CONF_BMC_VERIFY_SSL, default=current.get(CONF_BMC_VERIFY_SSL, DEFAULT_BMC_VERIFY_SSL)):       bool,
                vol.Optional(CONF_PI_ENABLED,     default=current.get(CONF_PI_ENABLED,     True)):                         bool,
                vol.Optional(CONF_PI_CPU_ENTITY,  default=current.get(CONF_PI_CPU_ENTITY,  DEFAULT_PI_CPU_ENTITY)):        str,
                vol.Optional(CONF_PI_TEMP_ENTITY, default=current.get(CONF_PI_TEMP_ENTITY, DEFAULT_PI_TEMP_ENTITY)):       str,
                vol.Optional(CONF_PI_MEM_ENTITY,  default=current.get(CONF_PI_MEM_ENTITY,  DEFAULT_PI_MEM_ENTITY)):        str,
                vol.Optional(CONF_PI_DISK_ENTITY, default=current.get(CONF_PI_DISK_ENTITY, DEFAULT_PI_DISK_ENTITY)):       str,
                vol.Optional(CONF_PI_NET_IN_ENTITY,  default=current.get(CONF_PI_NET_IN_ENTITY,  DEFAULT_PI_NET_IN_ENTITY)):  str,
                vol.Optional(CONF_PI_NET_OUT_ENTITY, default=current.get(CONF_PI_NET_OUT_ENTITY, DEFAULT_PI_NET_OUT_ENTITY)): str,
            }),
            errors=errors,
        )
