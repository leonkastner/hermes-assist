#!/usr/bin/env python3
"""Household ingress: checks the sender of an Assist message and calls the household Hermes API server.

Stdlib only. Config comes from the environment:
  ASSIST_KEY, API_SERVER_KEY, HERMES_URL, MEMBERS_FILE, BIND (comma list), PORT
  DELEGATE_KEY (optional; enables /v1/delegate)
  ADMIN_IDS (comma list of Home Assistant user ids that may use admin rights) and ROLE_FILE (default
  /run/household-role/role.json): without both, nobody is admin
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

log = logging.getLogger("household_ingress")

MAX_TEXT = 20000
MAX_IMAGES = 3
MAX_IMAGE_B64 = 6_000_000
MAX_ASSIST_BODY = MAX_IMAGES * MAX_IMAGE_B64 + MAX_TEXT * 4 + 4000
TOO_MANY_IMAGES = f"Höchstens {MAX_IMAGES} Fotos pro Frage."
ASSIST_TIMEOUT = 170
DELEGATE_TIMEOUT = 20 * 60
REFUSAL = "This assistant only answers Leon and Alina."
UNREACHABLE = "Sorry, I could not reach the household assistant right now. Please try again in a minute."


TZ_NAME = "Europe/Berlin"
ADMIN_WAIT = 10        # seconds an admin request waits for other requests to finish before it runs as a member
MEMBER_WAIT = 25       # seconds any other request waits for an admin request to finish
ROLE_MARGIN = 30       # the role file outlives the request timeout by this much
QUARANTINE_S = 900     # after a request ends without a reply, Hermes may still be running it: no admin until this passes
BUSY = "The household assistant is busy with another request. Please try again in a minute."
ADMIN_LINE = "[Access: admin. Leon's own admin rights are on for this message only.]\n"
NO_ADMIN_LINE = ("[Access: standard. Admin rights are not available for this message because another request was "
                 "running or has not finished cleanly. If the task needs them, tell Leon to ask again in a few minutes.]\n")


def time_line(now: datetime | None = None) -> str:
    """The api_server path gives the model no clock, so every forwarded message carries one."""
    try:
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TZ_NAME))
        return f"[Current time: {local:%A %Y-%m-%d %H:%M} {local.tzname()} ({TZ_NAME})]"
    except Exception:  # no tzdata on the host: say so rather than guess Berlin
        utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return f"[Current time: {utc:%A %Y-%m-%d %H:%M} UTC]"


def session_id(user_id: str, conversation_id: str) -> str:
    return "assist-" + hashlib.sha256(f"{user_id}:{conversation_id}".encode()).hexdigest()[:24]


class Flight:
    """Which requests are in flight. The guard sees one role file, so an admin request must run alone: it waits for
    the others to finish, and while it runs every other request (members, delegation) waits for it."""

    def __init__(self, clock=time.monotonic) -> None:
        self._cv = threading.Condition()
        self._active = 0
        self._admin = False
        self._clock = clock
        self._tainted_until = 0.0

    def taint(self, seconds: float = QUARANTINE_S) -> None:
        """A request ended without a reply (timeout, error): Hermes may keep running it, and the guard cannot tell
        its tool calls from a later request's. No admin rights until the quarantine passes."""
        with self._cv:
            self._tainted_until = max(self._tainted_until, self._clock() + seconds)

    def enter(self, want_admin: bool, admin_wait: float = ADMIN_WAIT, member_wait: float = MEMBER_WAIT) -> str | None:
        """Returns "admin", "member", or None (busy: an admin request did not finish within member_wait)."""
        with self._cv:
            if want_admin and self._clock() >= self._tainted_until:
                self._cv.wait_for(lambda: self._active == 0 and not self._admin, admin_wait)
                if self._active == 0 and not self._admin and self._clock() >= self._tainted_until:
                    self._admin = True
                    self._active = 1
                    return "admin"
            if not self._cv.wait_for(lambda: not self._admin, member_wait):
                return None
            self._active += 1
            return "member"

    def leave(self, role: str) -> None:
        with self._cv:
            if role == "admin":
                self._admin = False
            self._active -= 1
            self._cv.notify_all()


class RoleFile:
    """The role of the request in flight, for ha-guard. Written atomically, removed when the request ends."""

    def __init__(self, path: str | None) -> None:
        self.path = path

    def set_admin(self, ttl: float) -> None:
        d = os.path.dirname(self.path)
        tmp = os.path.join(d, f".role.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w") as f:
            json.dump({"role": "admin", "expires": time.time() + ttl}, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class Members:
    """members.json maps Home Assistant user id -> name; reloaded when the file's mtime changes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._mtime = None
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime_ns
                if mtime != self._mtime:
                    with open(self.path) as f:
                        self._data = {str(k): str(v) for k, v in json.load(f).items()}
                    self._mtime = mtime
            except (OSError, ValueError):
                self._data, self._mtime = {}, None
            return self._data.get(user_id)


def _call_hermes(cfg: dict, content: str | list, sid: str, timeout: float) -> str:
    body = json.dumps({"model": "household", "messages": [{"role": "user", "content": content}], "stream": False}).encode()
    req = urllib.request.Request(
        cfg["hermes_url"].rstrip("/") + "/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json",
                 "X-Hermes-Session-Id": sid, "X-Hermes-Session-Key": "household"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def _key_ok(header: str, key: str) -> bool:
    return bool(key) and hmac.compare_digest(header.encode(), f"Bearer {key}".encode())


def _image_parts(images) -> tuple[list[dict], list[str]] | tuple[int, str]:
    """Validated image_url parts and file names, or (status, message) when the list is refused."""
    if not isinstance(images, list):
        return 400, "Bad request."
    if len(images) > MAX_IMAGES:
        return 413, TOO_MANY_IMAGES
    parts, names = [], []
    for im in images:
        name, data = (im.get("name"), im.get("data")) if isinstance(im, dict) else (None, None)
        if not isinstance(name, str) or len(name) > 200 or not isinstance(data, str):
            return 400, "Bad request."
        if len(data) > MAX_IMAGE_B64:
            return 413, "That photo is too large."
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return 400, "Bad request."
        mime = "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/png" if raw.startswith(b"\x89PNG") else None
        if not mime:
            return 400, "Bad request."
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        names.append(name)
    return parts, names


def handle_assist(cfg: dict, body: dict) -> tuple[int, dict, str]:
    """Returns (status, json, who) for POST /v1/assist."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return 400, {"reply": "I did not get a message."}, "bad"
    if len(text) > MAX_TEXT:
        return 413, {"reply": "That message is too long."}, "bad"
    name = cfg["members"].get(body.get("user_id"))
    if not name:
        return 403, {"reply": REFUSAL}, "refused"
    parts, names = [], []
    if body.get("images"):
        checked = _image_parts(body["images"])
        if isinstance(checked[0], int):
            return checked[0], {"reply": checked[1]}, "bad"
        parts, names = checked
    sid = session_id(body["user_id"], str(body.get("conversation_id") or "default"))
    head = f"[Household member: {name}]\n{time_line()}\n"
    flight, roles = cfg["flight"], cfg["roles"]
    want_admin = body["user_id"] in cfg["admin_ids"] and roles.path is not None
    role = flight.enter(want_admin)
    if role is None:
        return 503, {"reply": BUSY}, name
    try:
        if role == "admin":
            try:
                roles.set_admin(ASSIST_TIMEOUT + ROLE_MARGIN)
            except OSError:
                log.exception("role file not writable; running as member")
                role = "member"
        if role == "admin":
            head += ADMIN_LINE
        elif want_admin:
            head += NO_ADMIN_LINE
        if parts:
            content = [{"type": "text", "text": f"{head}[Photos attached: {', '.join(names)}]\n{text}"}, *parts]
        else:
            content = head + text
        try:
            reply = _call_hermes(cfg, content, sid, ASSIST_TIMEOUT)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
            flight.taint()
            return 502, {"reply": UNREACHABLE}, name
        return 200, {"reply": reply}, f"{name}{' admin' if role == 'admin' else ''}"
    finally:
        if role == "admin":
            roles.clear()
        flight.leave(role)


def handle_delegate(cfg: dict, body: dict) -> tuple[int, dict, str]:
    card, title = str(body.get("card_id") or ""), str(body.get("title") or "")
    if not card or not title:
        return 400, {"status": "blocked", "text": "card_id and title are required"}, "bad"
    content = (f"[Delegated task from Leon's personal assistant, card {card}]\n{time_line()}\n"
               f"{title}\n\n{body.get('body') or ''}")
    role = cfg["flight"].enter(False, member_wait=cfg.get("delegate_wait", DELEGATE_TIMEOUT))
    if role is None:
        return 503, {"status": "blocked", "text": BUSY}, "delegate"
    try:
        reply = _call_hermes(cfg, content, f"card-{card}", cfg.get("delegate_timeout", DELEGATE_TIMEOUT))
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        cfg["flight"].taint()
        return 502, {"status": "blocked", "text": "household assistant unreachable"}, "delegate"
    finally:
        cfg["flight"].leave(role)
    first = reply.lstrip().split("\n", 1)[0]
    status = "blocked" if first.startswith("BLOCKED:") else "done"
    return 200, {"status": status, "text": reply}, "delegate"


# path -> (config key holding the bearer key, handler). Adding an endpoint is one entry.
ROUTES = {
    "/v1/assist": ("assist_key", handle_assist),
    "/v1/delegate": ("delegate_key", handle_delegate),
}


def make_handler(cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default access log
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False})

        def do_POST(self):
            t0 = time.time()
            route = ROUTES.get(self.path)
            if not route:
                return self._send(404, {"ok": False})
            key_name, fn = route
            if not _key_ok(self.headers.get("Authorization", ""), cfg.get(key_name, "")):
                log.info("%s refused status=401", self.path)
                return self._send(401, {"ok": False})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > (MAX_ASSIST_BODY if self.path == "/v1/assist" else MAX_TEXT * 4 + 2000):
                    return self._send(413, {"reply": "That message is too long."})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"reply": "Bad request."})
            code, out, who = fn(cfg, body)
            imgs = body.get("images")
            log.info("%s member=%s status=%d images=%d ms=%d", self.path, who, code,
                     len(imgs) if isinstance(imgs, list) else 0, (time.time() - t0) * 1000)
            self._send(code, out)

    return Handler


def serve(cfg: dict, binds: list[str], port: int) -> list[ThreadingHTTPServer]:
    servers = []
    for host in binds:
        srv = ThreadingHTTPServer((host, port), make_handler(cfg))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers


class _Cfg(dict):
    """dict whose 'members' entry is the live Members object."""


def load_cfg() -> _Cfg:
    e = os.environ
    members = Members(e.get("MEMBERS_FILE", "/etc/household-ingress/members.json"))

    class MembersView:
        def get(self, user_id):
            return members.get(user_id)

    admin_ids = {i.strip() for i in e.get("ADMIN_IDS", "").split(",") if i.strip()}
    roles = RoleFile(e.get("ROLE_FILE", "/run/household-role/role.json") if admin_ids else None)
    if roles.path:
        roles.clear()  # a file left by a crash must not outlive the process that wrote it
    return _Cfg(assist_key=e["ASSIST_KEY"], api_key=e["API_SERVER_KEY"], delegate_key=e.get("DELEGATE_KEY", ""),
                hermes_url=e.get("HERMES_URL", "http://127.0.0.1:8809"), members=MembersView(),
                admin_ids=admin_ids, roles=roles, flight=Flight())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_cfg()
    binds = [b.strip() for b in os.environ.get("BIND", "127.0.0.1").split(",") if b.strip()]
    serve(cfg, binds, int(os.environ.get("PORT", "8808")))
    threading.Event().wait()


if __name__ == "__main__":
    main()
