"""Photos a household member uploaded to Local media, for their next Assist question."""
from __future__ import annotations

import base64
import io
import re
import shutil
import time
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageOps, UnidentifiedImageError

FOLDER = "Household"
MAX_PHOTOS = 3
WINDOW_S = 15 * 60
MAX_EDGE = 2048
KEEP_SENT_S = 30 * 86400


class Photo(NamedTuple):
    path: Path
    name: str
    data_b64: str


def folder(root: Path, member: str) -> Path:
    safe = re.sub(r"[^\w .-]", "_", member).replace("..", "_").strip(" .") or "member"
    return Path(root) / FOLDER / safe


def _encode(path: Path) -> str:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def collect(root: Path, member: str, now: float | None = None) -> tuple[list[Photo], list[str]]:
    now = time.time() if now is None else now
    d = folder(root, member)
    d.mkdir(parents=True, exist_ok=True)
    files = sorted((p for p in d.iterdir() if p.is_file() and now - p.stat().st_mtime <= WINDOW_S),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    photos, notes = [], []
    for p in files:
        if len(photos) == MAX_PHOTOS:
            break
        try:
            photos.append(Photo(p, p.name, _encode(p)))
        except (UnidentifiedImageError, OSError, ValueError):
            notes.append(f"Foto {p.name} konnte nicht gelesen werden.")
    return photos, notes


def archive(photos: list[Photo]) -> None:
    for ph in photos:
        sent = ph.path.parent / "sent"
        sent.mkdir(exist_ok=True)
        shutil.move(str(ph.path), str(sent / ph.path.name))


def prune(root: Path, member: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    sent = folder(root, member) / "sent"
    if sent.is_dir():
        for p in sent.iterdir():
            if p.is_file() and now - p.stat().st_mtime > KEEP_SENT_S:
                p.unlink(missing_ok=True)
