#!/usr/bin/env python3
"""Household voice: a Wyoming bridge from Home Assistant to Mistral Voxtral.

Two Wyoming servers: speech-to-text (STT_PORT) and text-to-speech (TTS_PORT). Config comes from the environment:
  MISTRAL_API_KEY, BIND, STT_PORT, TTS_PORT, MISTRAL_STT_MODEL, MISTRAL_TTS_MODEL, MISTRAL_VOICE
  MISTRAL_BASE (default https://api.mistral.ai), MISTRAL_TIMEOUT (seconds, default 20)
Nothing is stored; per request only direction, size, Mistral status and latency are logged.
"""
from __future__ import annotations

import array
import asyncio
import base64
import io
import logging
import os
import re
import struct
import time
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, AsrModel, AsrProgram, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.tts import Synthesize

log = logging.getLogger("household_voice")

MAX_AUDIO_S = 30
MAX_SPOKEN_CHARS = 1000
CHUNK_SAMPLES = 1024
LANG = "de"
TTS_LANG = "de_DE"
TZ_NAME = "Europe/Berlin"
ATTRIBUTION = Attribution(name="Mistral AI", url="https://mistral.ai")


class MistralError(Exception):
    pass


def _cfg(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise MistralError(f"{name} is not set")
    return value


def pcm_to_wav(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _post(path: str, **kwargs) -> dict:
    url = _cfg("MISTRAL_BASE", "https://api.mistral.ai").rstrip("/") + path
    headers = {"Authorization": f"Bearer {_cfg('MISTRAL_API_KEY')}"}
    t0 = time.time()
    try:
        r = httpx.post(url, headers=headers, timeout=float(_cfg("MISTRAL_TIMEOUT", "20")), **kwargs)
    except httpx.HTTPError as e:
        log.warning("mistral %s failed: %s after %.1fs", path, type(e).__name__, time.time() - t0)
        raise MistralError(type(e).__name__) from e
    log.info("mistral %s status=%s latency=%.2fs", path, r.status_code, time.time() - t0)
    if r.status_code != 200:
        raise MistralError(str(r.status_code))
    try:
        return r.json()
    except ValueError as e:
        raise MistralError("bad response") from e


def transcribe(pcm: bytes, rate: int, width: int, channels: int) -> str:
    pcm = pcm[: rate * width * channels * MAX_AUDIO_S]
    wav = pcm_to_wav(pcm, rate, width, channels)
    out = _post("/v1/audio/transcriptions",
                data={"model": _cfg("MISTRAL_STT_MODEL", "voxtral-mini-latest"), "language": LANG},
                files={"file": ("audio.wav", wav, "audio/wav")})
    log.info("stt seconds=%.1f", len(pcm) / (rate * width * channels))
    return str(out.get("text", "")).strip()


def speakable(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(re.finditer(r"[.!?](?=\s|$)", cut))
    if ends:
        return cut[: ends[-1].end()]
    return cut.rsplit(" ", 1)[0].rstrip() if " " in cut else cut


def _wav_to_int16(wav: bytes) -> tuple[bytes, int]:
    """Mono int16 PCM and its rate from a RIFF/WAVE file (PCM 16 bit, or IEEE float 32 bit)."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise MistralError("not a wav")
    fmt = data = None
    pos = 12
    while pos + 8 <= len(wav):
        cid, size = wav[pos:pos + 4], struct.unpack("<I", wav[pos + 4:pos + 8])[0]
        body = wav[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
            if fmt[0] == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE: real tag leads the sub-format
                fmt = (struct.unpack("<H", body[24:26])[0],) + fmt[1:]
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise MistralError("bad wav")
    tag, channels, rate, _, _, bits = fmt
    if tag == 3 and bits == 32:
        floats = array.array("f")
        floats.frombytes(data[: len(data) // 4 * 4])
        samples = array.array("h", (max(-32768, min(32767, int(s * 32767))) for s in floats))
    elif tag == 1 and bits == 16:
        samples = array.array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
    else:
        raise MistralError(f"unsupported wav format {tag}/{bits}")
    if channels > 1:
        samples = samples[::channels]
    return samples.tobytes(), rate


def synthesize(text: str) -> tuple[bytes, int]:
    out = _post("/v1/audio/speech",
                json={"input": text, "model": _cfg("MISTRAL_TTS_MODEL", "voxtral-mini-tts-latest"),
                      "voice_id": _cfg("MISTRAL_VOICE"), "response_format": "wav"})
    try:
        wav = base64.b64decode(out["audio_data"])
    except (KeyError, ValueError) as e:
        raise MistralError("bad response") from e
    log.info("tts chars=%d", len(text))
    return _wav_to_int16(wav)


class _Daily:
    """One summary line at the first request of each Berlin day."""

    def __init__(self) -> None:
        self.day = self._today()
        self.stt_seconds = self.tts_chars = self.errors = 0.0

    @staticmethod
    def _today() -> str:
        return datetime.now(ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d")

    def add(self, stt_seconds: float = 0, tts_chars: int = 0, errors: int = 0) -> None:
        today = self._today()
        if today != self.day:
            log.info("day=%s stt_seconds=%.0f tts_chars=%d errors=%d", self.day, self.stt_seconds, self.tts_chars, self.errors)
            self.day = today
            self.stt_seconds = self.tts_chars = self.errors = 0.0
        self.stt_seconds += stt_seconds
        self.tts_chars += tts_chars
        self.errors += errors


DAILY = _Daily()


class SttHandler(AsyncEventHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pcm = bytearray()
        self.fmt = (16000, 2, 1)

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(_info(asr=True).event())
        elif AudioStart.is_type(event.type):
            a = AudioStart.from_event(event)
            self.fmt, self.pcm = (a.rate, a.width, a.channels), bytearray()
        elif AudioChunk.is_type(event.type):
            if len(self.pcm) < self.fmt[0] * self.fmt[1] * self.fmt[2] * MAX_AUDIO_S:
                self.pcm += AudioChunk.from_event(event).audio
        elif AudioStop.is_type(event.type):
            await self._finish()
        return True

    async def _finish(self) -> None:
        pcm, self.pcm = bytes(self.pcm), bytearray()
        try:
            text = await asyncio.to_thread(transcribe, pcm, *self.fmt)
        except MistralError as e:
            log.warning("stt error: %s", e)
            DAILY.add(errors=1)
            await self.write_event(Error(text=f"Mistral speech failed ({e})").event())
            return
        DAILY.add(stt_seconds=len(pcm) / (self.fmt[0] * self.fmt[1] * self.fmt[2]))
        await self.write_event(Transcript(text=text, language=LANG).event())


class TtsHandler(AsyncEventHandler):
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(_info(asr=False).event())
        elif Synthesize.is_type(event.type):
            text = speakable(Synthesize.from_event(event).text)
            try:
                pcm, rate = await asyncio.to_thread(synthesize, text)
            except MistralError as e:
                log.warning("tts error: %s", e)
                DAILY.add(errors=1)
                await self.write_event(Error(text=f"Mistral speech failed ({e})").event())
                return True
            DAILY.add(tts_chars=len(text))
            await self.write_event(AudioStart(rate, 2, 1).event())
            step = CHUNK_SAMPLES * 2
            for i in range(0, len(pcm), step):
                await self.write_event(AudioChunk(rate, 2, 1, pcm[i:i + step]).event())
            await self.write_event(AudioStop().event())
        return True


def _info(asr: bool) -> Info:
    if asr:
        model = AsrModel(name="voxtral", attribution=ATTRIBUTION, installed=True, description="Mistral Voxtral",
                         version=None, languages=[LANG])
        return Info(asr=[AsrProgram(name="household-voice-stt", attribution=ATTRIBUTION, installed=True,
                                    description="Speech to text via Mistral Voxtral", version=None, models=[model],
                                    requires_external_vad=False)])
    voice = TtsVoice(name=os.environ.get("MISTRAL_VOICE", "default"), attribution=ATTRIBUTION, installed=True,
                     description="Mistral Voxtral voice", version=None, languages=[TTS_LANG])
    return Info(tts=[TtsProgram(name="household-voice-tts", attribution=ATTRIBUTION, installed=True,
                                description="Text to speech via Mistral Voxtral", version=None, voices=[voice])])


class Service:
    """One Wyoming TCP server; `port` holds the bound port after start (port 0 picks a free one)."""

    def __init__(self, handler, host: str, port: int) -> None:
        self.handler, self.port = handler, port
        self._srv = AsyncTcpServer(host, port)

    async def start(self) -> None:
        await self._srv.start(self.handler)
        self.port = self._srv._server.sockets[0].getsockname()[1]

    async def run(self) -> None:
        await self._srv.run(self.handler)

    async def stop(self) -> None:
        await self._srv.stop()


def build_servers(host: str, stt_port: int, tts_port: int) -> tuple[Service, Service]:
    return Service(SttHandler, host, stt_port), Service(TtsHandler, host, tts_port)


async def amain() -> None:
    host = _cfg("BIND")
    stt, tts = build_servers(host, int(_cfg("STT_PORT", "8822")), int(_cfg("TTS_PORT", "8823")))
    log.info("serving stt %s:%s tts %s:%s", host, stt.port, host, tts.port)
    await asyncio.gather(stt.run(), tts.run())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
