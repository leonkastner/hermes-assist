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


def _jpeg(path, age=60):
    import os
    import time
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "red").save(path, "JPEG")
    t = time.time() - age
    os.utime(path, (t, t))


async def _as_leon(hass, tmp_path):
    from homeassistant.auth.models import Credentials  # noqa: F401
    user = await hass.auth.async_create_user("Leon")
    hass.config.media_dirs = {"local": str(tmp_path)}
    return user


async def test_recent_photo_is_sent_and_archived(hass, aioclient_mock, tmp_path):
    aioclient_mock.post(f"{URL}/v1/assist", json={"reply": "Ein roter Fleck."})
    entity_id = await _setup(hass)
    user = await _as_leon(hass, tmp_path)
    _jpeg(tmp_path / "Household" / "Leon" / "a.jpg")
    await conversation.async_converse(
        hass, "Was ist auf dem Foto?", None, Context(user_id=user.id), agent_id=entity_id, language="de")
    payload = aioclient_mock.mock_calls[0][2]
    assert payload["images"][0]["name"] == "a.jpg" and payload["images"][0]["data"]
    assert (tmp_path / "Household" / "Leon" / "sent" / "a.jpg").exists()


async def test_photos_kept_when_ingress_fails(hass, aioclient_mock, tmp_path):
    aioclient_mock.post(f"{URL}/v1/assist", status=502, json={"reply": "unreachable"})
    entity_id = await _setup(hass)
    user = await _as_leon(hass, tmp_path)
    _jpeg(tmp_path / "Household" / "Leon" / "a.jpg")
    await conversation.async_converse(hass, "hi", None, Context(user_id=user.id), agent_id=entity_id)
    assert (tmp_path / "Household" / "Leon" / "a.jpg").exists()


async def test_no_user_sends_no_images(hass, aioclient_mock, tmp_path):
    aioclient_mock.post(f"{URL}/v1/assist", status=403, json={"reply": "no"})
    entity_id = await _setup(hass)
    hass.config.media_dirs = {"local": str(tmp_path)}
    await conversation.async_converse(hass, "hi", None, Context(), agent_id=entity_id)
    assert "images" not in aioclient_mock.mock_calls[0][2]
    assert not (tmp_path / "Household").exists()


async def test_notes_are_prepended_to_text(hass, aioclient_mock, tmp_path):
    import os
    aioclient_mock.post(f"{URL}/v1/assist", json={"reply": "ok"})
    entity_id = await _setup(hass)
    user = await _as_leon(hass, tmp_path)
    bad = tmp_path / "Household" / "Leon" / "x.heic"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(os.urandom(100))
    await conversation.async_converse(hass, "Frage", None, Context(user_id=user.id), agent_id=entity_id)
    payload = aioclient_mock.mock_calls[0][2]
    assert payload["text"].startswith("Foto x.heic") and payload["text"].endswith("Frage")
    assert "images" not in payload
