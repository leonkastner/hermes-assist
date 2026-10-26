import array
import asyncio
import base64
import io
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "voice"))
import household_voice as hv  # noqa: E402

KEY = "mistral-test-key"
SECRET_TEXT = "geheimer Satz"


def make_wav(samples: list[float], rate: int = 24000, floats: bool = False) -> bytes:
    if floats:
        data = array.array("f", samples).tobytes()
        fmt, bits = 3, 32
    else:
        data = array.array("h", [int(s * 32767) for s in samples]).tobytes()
        fmt, bits = 1, 16
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, fmt, 1, rate, rate * bits // 8, bits // 8, bits) + b"data" + struct.pack("<I", len(data))
    return hdr + data


class FakeMistral:
    def __init__(self):
        self.requests = []
        self.stt_text = SECRET_TEXT
        self.script = []  # status codes consumed per request; 200 when empty; "slow" sleeps
        self.tts_wav = make_wav([0.1] * 2400, 24000)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                step = outer.script.pop(0) if outer.script else 200
                if step == "slow":
                    time.sleep(3)
                    step = 200
                if step != 200:
                    self.send_response(step)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                if self.path == "/v1/audio/transcriptions":
                    out = {"text": outer.stt_text}
                else:
                    out = {"audio_data": base64.b64encode(outer.tts_wav).decode()}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def uploaded_wav(self, i=-1):
        body = self.requests[i]["body"]
        m = re.search(rb'name="file"[^\r]*\r\nContent-Type: [^\r]*\r\n\r\n(.*?)\r\n--', body, re.S)
        return m.group(1)

    def field(self, name, i=-1):
        m = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n--', self.requests[i]["body"], re.S)
        return m.group(1).decode()


@pytest.fixture
def fake(monkeypatch):
    f = FakeMistral()
    monkeypatch.setenv("MISTRAL_API_KEY", KEY)
    monkeypatch.setenv("MISTRAL_BASE", f.url)
    monkeypatch.setenv("MISTRAL_STT_MODEL", "stt-model")
    monkeypatch.setenv("MISTRAL_TTS_MODEL", "tts-model")
    monkeypatch.setenv("MISTRAL_VOICE", "de-voice")
    monkeypatch.setenv("MISTRAL_TIMEOUT", "1")
    yield f
    f.srv.shutdown()
    f.srv.server_close()


def test_transcribe_sends_wav_and_language(fake):
    pcm = array.array("h", [100] * 16000).tobytes()
    assert hv.transcribe(pcm, 16000, 2, 1) == SECRET_TEXT
    req = fake.requests[0]
    assert req["path"] == "/v1/audio/transcriptions"
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert fake.field("language") == "de"
    assert fake.field("model") == "stt-model"
    with wave.open(io.BytesIO(fake.uploaded_wav())) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 16000)


def test_audio_capped_at_30s(fake):
    pcm = bytes(16000 * 2 * 120)
    hv.transcribe(pcm, 16000, 2, 1)
    with wave.open(io.BytesIO(fake.uploaded_wav())) as w:
        assert w.getnframes() == 16000 * 30


@pytest.mark.parametrize("floats", [False, True])
def test_synthesize_returns_int16(fake, floats):
    fake.tts_wav = make_wav([0.5] * 4800, 24000, floats=floats)
    pcm, rate = hv.synthesize("Hallo")
    assert rate == 24000 and len(pcm) == 4800 * 2
    first = array.array("h", pcm[:2])[0]
    assert abs(first - 16383) < 5
    body = json.loads(fake.requests[0]["body"])
    assert body["input"] == "Hallo" and body["voice_id"] == "de-voice" and body["model"] == "tts-model"
    assert body["response_format"] == "wav"
    assert fake.requests[0]["path"] == "/v1/audio/speech"


def test_reply_cut_at_sentence():
    text = "Das ist ein Satz. " * 300
    out = hv.speakable(text)
    assert len(out) <= 1000 and out.endswith(".")
    assert hv.speakable("wort " * 500).endswith("wort")
    assert hv.speakable("Kurz.") == "Kurz."
    assert len(hv.speakable("wort " * 500)) <= 1000


async def start_servers():
    stt, tts = hv.build_servers("127.0.0.1", 0, 0)
    await stt.start()
    await tts.start()
    return stt, tts


async def asr_request(port, pcm):
    async with AsyncTcpClient("127.0.0.1", port) as c:
        await c.write_event(Transcribe(language="de").event())
        await c.write_event(AudioStart(16000, 2, 1).event())
        await c.write_event(AudioChunk(16000, 2, 1, pcm).event())
        await c.write_event(AudioStop().event())
        return await asyncio.wait_for(c.read_event(), 25)


async def test_mistral_failure_is_wyoming_error(fake):
    fake.script = [429, 500, "slow"]
    stt, tts = await start_servers()
    try:
        pcm = bytes(3200)
        for _ in range(3):
            t0 = time.time()
            ev = await asr_request(stt.port, pcm)
            assert Error.is_type(ev.type)
            assert time.time() - t0 < 21
        ev = await asr_request(stt.port, pcm)
        assert Transcript.is_type(ev.type) and Transcript.from_event(ev).text == SECRET_TEXT
    finally:
        await stt.stop()
        await tts.stop()


async def test_wyoming_roundtrip_and_no_logging(fake, caplog):
    caplog.set_level(logging.DEBUG)
    stt, tts = await start_servers()
    try:
        async with AsyncTcpClient("127.0.0.1", stt.port) as c:
            await c.write_event(Describe().event())
            info = Info.from_event(await c.read_event())
            assert len(info.asr) == 1 and "de" in info.asr[0].models[0].languages and not info.tts
        async with AsyncTcpClient("127.0.0.1", tts.port) as c:
            await c.write_event(Describe().event())
            info = Info.from_event(await c.read_event())
            assert len(info.tts) == 1 and info.tts[0].voices[0].languages == ["de_DE"]
        ev = await asr_request(stt.port, array.array("h", [500] * 16000).tobytes())
        assert Transcript.from_event(ev).text == SECRET_TEXT
        async with AsyncTcpClient("127.0.0.1", tts.port) as c:
            await c.write_event(Synthesize(text=SECRET_TEXT).event())
            types = []
            while True:
                ev = await asyncio.wait_for(c.read_event(), 10)
                types.append(ev.type)
                if AudioStop.is_type(ev.type):
                    break
        assert types[0] == "audio-start" and types.count("audio-chunk") >= 2
    finally:
        await stt.stop()
        await tts.stop()
    assert SECRET_TEXT not in caplog.text and KEY not in caplog.text
    assert "stt" in caplog.text.lower()
