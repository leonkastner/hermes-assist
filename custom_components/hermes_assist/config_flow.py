from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_KEY, CONF_URL, DOMAIN

SCHEMA = vol.Schema({vol.Required(CONF_URL): str, vol.Required(CONF_KEY): str})


class HermesAssistConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            try:
                async with async_get_clientsession(self.hass).get(
                    f"{url}/health", timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    ok = resp.status == 200 and (await resp.json()).get("ok") is True
            except (aiohttp.ClientError, TimeoutError, ValueError):
                ok = False
            if ok:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Hermes Assist", data={CONF_URL: url, CONF_KEY: user_input[CONF_KEY]})
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
