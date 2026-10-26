import base64
import io
import os

import pytest
from PIL import Image

from custom_components.hermes_assist import photos

NOW = 1_800_000_000.0


def _img(path, size=(40, 30), fmt="JPEG", age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "red").save(path, fmt)
    os.utime(path, (NOW - age, NOW - age))
    return path


def _decode(photo):
    return Image.open(io.BytesIO(base64.b64decode(photo.data_b64)))


def test_collects_recent_photos_newest_first_max_three(tmp_path):
    for i in range(5):
        _img(tmp_path / "Household" / "Leon" / f"p{i}.jpg", age=60 * (i + 1))
    got, notes = photos.collect(tmp_path, "Leon", NOW)
    assert [p.name for p in got] == ["p0.jpg", "p1.jpg", "p2.jpg"] and notes == []


def test_ignores_old_and_sent(tmp_path):
    _img(tmp_path / "Household" / "Leon" / "old.jpg", age=16 * 60)
    _img(tmp_path / "Household" / "Leon" / "sent" / "done.jpg", age=30)
    assert photos.collect(tmp_path, "Leon", NOW) == ([], [])


def test_other_members_upload_not_attached(tmp_path):
    _img(tmp_path / "Household" / "Alina" / "a.jpg", age=30)
    assert photos.collect(tmp_path, "Leon", NOW) == ([], [])


def test_large_image_is_shrunk_to_2048(tmp_path):
    _img(tmp_path / "Household" / "Leon" / "big.png", size=(4000, 3000), fmt="PNG")
    got, _ = photos.collect(tmp_path, "Leon", NOW)
    im = _decode(got[0])
    assert im.format == "JPEG" and im.size == (2048, 1536)


def test_unreadable_image_is_reported_not_sent(tmp_path):
    bad = tmp_path / "Household" / "Leon" / "x.heic"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(os.urandom(200))
    os.utime(bad, (NOW - 10, NOW - 10))
    got, notes = photos.collect(tmp_path, "Leon", NOW)
    assert got == [] and len(notes) == 1 and "x.heic" in notes[0] and bad.exists()


def test_archive_moves_to_sent(tmp_path):
    _img(tmp_path / "Household" / "Leon" / "a.jpg")
    got, _ = photos.collect(tmp_path, "Leon", NOW)
    photos.archive(got)
    assert not (tmp_path / "Household" / "Leon" / "a.jpg").exists()
    assert (tmp_path / "Household" / "Leon" / "sent" / "a.jpg").exists()


def test_prune_deletes_after_30_days(tmp_path):
    _img(tmp_path / "Household" / "Leon" / "sent" / "old.jpg", age=31 * 86400)
    _img(tmp_path / "Household" / "Leon" / "sent" / "new.jpg", age=86400)
    photos.prune(tmp_path, "Leon", NOW)
    sent = tmp_path / "Household" / "Leon" / "sent"
    assert not (sent / "old.jpg").exists() and (sent / "new.jpg").exists()


def test_member_name_is_a_safe_folder_name(tmp_path):
    d = photos.folder(tmp_path, "../etc")
    assert d.parent == tmp_path / "Household" and ".." not in d.name and "/" not in d.name
