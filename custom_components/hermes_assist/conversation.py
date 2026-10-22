from __future__ import annotations

import asyncio

import aiohttp
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_KEY, CONF_URL


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([HermesAssistEntity(entry)])


class HermesAssistEntity(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = False

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> str:
        return MATCH_ALL

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: conversation.ChatLog
    ) -> conversation.ConversationResult:
        session = async_get_clientsession(self.hass)
        payload = {
            "text": user_input.text,
            "conversation_id": chat_log.conversation_id,
            "user_id": user_input.context.user_id,
            "language": user_input.language,
        }
        try:
            async with session.post(
                f"{self.entry.data[CONF_URL]}/v1/assist",
                json=payload,
                headers={"Authorization": f"Bearer {self.entry.data[CONF_KEY]}"},
                timeout=aiohttp.ClientTimeout(total=170),
            ) as resp:
                reply = (await resp.json()).get("reply") or "The household assistant sent no answer."
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            reply = "The household assistant is not reachable right now."
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=reply)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
