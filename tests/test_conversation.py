import pytest
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hermes_assist.const import CONF_KEY, CONF_URL, DOMAIN

URL = "http://ingress.test:8808"


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: URL, CONF_KEY: "k"})
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)


async def test_sender_user_id_forwarded_and_reply_spoken(hass, aioclient_mock):
    aioclient_mock.post(f"{URL}/v1/assist", json={"reply": "Licht ist wärmer."})
    entity_id = await _setup(hass)
    result = await conversation.async_converse(
        hass, "Licht wärmer", None, Context(user_id="uLeon"), agent_id=entity_id, language="de")
    assert result.response.speech["plain"]["speech"] == "Licht ist wärmer."
    _, _, payload, headers = aioclient_mock.mock_calls[0]
    assert payload["user_id"] == "uLeon" and payload["text"] == "Licht wärmer"
    assert headers["Authorization"] == "Bearer k"


async def test_refusal_shown_as_speech(hass, aioclient_mock):
    aioclient_mock.post(f"{URL}/v1/assist", status=403, json={"reply": "This assistant only answers Leon and Alina."})
    entity_id = await _setup(hass)
    result = await conversation.async_converse(hass, "hi", None, Context(user_id="uX"), agent_id=entity_id)
    assert "only answers Leon and Alina" in result.response.speech["plain"]["speech"]


async def test_no_user_id_is_sent_as_null(hass, aioclient_mock):
    aioclient_mock.post(f"{URL}/v1/assist", status=403, json={"reply": "no"})
    entity_id = await _setup(hass)
    await conversation.async_converse(hass, "hi", None, Context(), agent_id=entity_id)
    assert aioclient_mock.mock_calls[0][2]["user_id"] is None
