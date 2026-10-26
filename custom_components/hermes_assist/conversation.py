from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import photos as photos_mod
from .const import CONF_KEY, CONF_URL

_LOGGER = logging.getLogger(__name__)


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
        photos, member = await self._collect_photos(user_input.context.user_id, payload)
        try:
            async with session.post(
                f"{self.entry.data[CONF_URL]}/v1/assist",
                json=payload,
                headers={"Authorization": f"Bearer {self.entry.data[CONF_KEY]}"},
                timeout=aiohttp.ClientTimeout(total=170),
            ) as resp:
                reply = (await resp.json()).get("reply") or "The household assistant sent no answer."
                sent_ok = resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            reply = "The household assistant is not reachable right now."
            sent_ok = False
        if sent_ok:
            await self._archive_photos(photos, member)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=reply)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _collect_photos(self, user_id: str | None, payload: dict) -> tuple[list, str | None]:
        """Add the member's recent Local media photos to the payload. Never blocks the question."""
        root = self.hass.config.media_dirs.get("local")
        if not user_id or not root:
            return [], None
        try:
            user = await self.hass.auth.async_get_user(user_id)
            if user is None or not user.name:
                return [], None
            found, notes = await self.hass.async_add_executor_job(photos_mod.collect, Path(root), user.name)
        except Exception:  # a photo problem must not stop a text question
            _LOGGER.exception("Collecting photos failed")
            return [], None
        if found:
            payload["images"] = [{"name": p.name, "data": p.data_b64} for p in found]
        if notes:
            payload["text"] = "\n".join(notes) + "\n" + payload["text"]
        return found, user.name

    async def _archive_photos(self, found: list, member: str | None) -> None:
        if not member:
            return
        root = Path(self.hass.config.media_dirs["local"])
        try:
            await self.hass.async_add_executor_job(photos_mod.archive, found)
            await self.hass.async_add_executor_job(photos_mod.prune, root, member)
        except Exception:
            _LOGGER.exception("Archiving photos failed")
