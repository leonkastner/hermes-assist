#!/usr/bin/env python3
"""Household ingress: checks the sender of an Assist message and calls the household Hermes API server.

Stdlib only. Config comes from the environment:
  ASSIST_KEY, API_SERVER_KEY, HERMES_URL, MEMBERS_FILE, BIND (comma list), PORT
  DELEGATE_KEY (optional; enables /v1/delegate)
"""
from __future__ import annotations

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
ASSIST_TIMEOUT = 170
DELEGATE_TIMEOUT = 20 * 60
REFUSAL = "This assistant only answers Leon and Alina."
UNREACHABLE = "Sorry, I could not reach the household assistant right now. Please try again in a minute."


TZ_NAME = "Europe/Berlin"


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


def _call_hermes(cfg: dict, content: str, sid: str, timeout: float) -> str:
    body = json.dumps({"model": "household", "messages": [{"role": "user", "content": content}], "stream": False}).encode()
    req = urllib.request.Request(
        cfg["hermes_url"].rstrip("/") + "/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json",
                 "X-Hermes-Session-Id": sid, "X-Hermes-Session-Key": "household"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def _key_ok(header: str, key: str) -> bool:
    return bool(key) and hmac.compare_digest(header.encode(), f"Bearer {key}".encode())


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
    sid = session_id(body["user_id"], str(body.get("conversation_id") or "default"))
    try:
        reply = _call_hermes(cfg, f"[Household member: {name}]\n{time_line()}\n{text}", sid, ASSIST_TIMEOUT)
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return 502, {"reply": UNREACHABLE}, name
    return 200, {"reply": reply}, name


def handle_delegate(cfg: dict, body: dict) -> tuple[int, dict, str]:
    card, title = str(body.get("card_id") or ""), str(body.get("title") or "")
    if not card or not title:
        return 400, {"status": "blocked", "text": "card_id and title are required"}, "bad"
    content = f"[Delegated task from Leon's personal assistant, card {card}]\n{title}\n\n{body.get('body') or ''}"
    try:
        reply = _call_hermes(cfg, content, f"card-{card}", cfg.get("delegate_timeout", DELEGATE_TIMEOUT))
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return 502, {"status": "blocked", "text": "household assistant unreachable"}, "delegate"
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
                if n > MAX_TEXT * 4 + 2000:
                    return self._send(413, {"reply": "That message is too long."})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"reply": "Bad request."})
            code, out, who = fn(cfg, body)
            log.info("%s member=%s status=%d ms=%d", self.path, who, code, (time.time() - t0) * 1000)
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

    return _Cfg(assist_key=e["ASSIST_KEY"], api_key=e["API_SERVER_KEY"], delegate_key=e.get("DELEGATE_KEY", ""),
                hermes_url=e.get("HERMES_URL", "http://127.0.0.1:8809"), members=MembersView())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_cfg()
    binds = [b.strip() for b in os.environ.get("BIND", "127.0.0.1").split(",") if b.strip()]
    serve(cfg, binds, int(os.environ.get("PORT", "8808")))
    threading.Event().wait()


if __name__ == "__main__":
    main()
