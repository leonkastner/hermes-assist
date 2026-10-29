import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingress"))
import household_ingress as hi  # noqa: E402

ASSIST, API, DELEGATE = "assist-key", "api-key", "delegate-key"


class FakeHermes:
    def __init__(self):
        self.last = None
        self.reply = "fake reply"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.last = type("R", (), {"headers": dict(self.headers), "json": json.loads(self.rfile.read(n))})
                data = json.dumps({"choices": [{"message": {"content": outer.reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def post(port, path, body, key):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class IngressTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHermes()
        self.dir = tempfile.TemporaryDirectory()
        self.members_path = os.path.join(self.dir.name, "members.json")
        with open(self.members_path, "w") as f:
            json.dump({"uLeon": "Leon"}, f)
        os.environ.update(ASSIST_KEY=ASSIST, API_SERVER_KEY=API, DELEGATE_KEY=DELEGATE,
                          HERMES_URL=f"http://127.0.0.1:{self.fake.port}", MEMBERS_FILE=self.members_path)
        self.servers = hi.serve(hi.load_cfg(), ["127.0.0.1"], 0)
        self.port = self.servers[0].server_address[1]

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.fake.stop()
        self.dir.cleanup()

    def post(self, path, body, key=ASSIST):
        return post(self.port, path, body, key)

    def test_member_message_forwarded_with_sender_and_session(self):
        code, body = self.post("/v1/assist", {"text": "warmer evening light", "conversation_id": "c1", "user_id": "uLeon", "language": "de"})
        self.assertEqual(code, 200)
        self.assertEqual(body["reply"], "fake reply")
        req = self.fake.last
        self.assertEqual(req.headers["X-Hermes-Session-Key"], "household")
        self.assertTrue(req.headers["X-Hermes-Session-Id"].startswith("assist-"))
        self.assertEqual(req.headers["Authorization"], f"Bearer {API}")
        self.assertTrue(req.json["messages"][0]["content"].startswith("[Household member: Leon]\n"))

    def test_time_line_is_berlin_with_weekday_and_dst(self):
        from datetime import datetime, timezone
        self.assertEqual(hi.time_line(datetime(2026, 10, 24, 9, 12, tzinfo=timezone.utc)),
                         "[Current time: Saturday 2026-10-24 11:12 CEST (Europe/Berlin)]")
        self.assertEqual(hi.time_line(datetime(2026, 12, 24, 9, 12, tzinfo=timezone.utc)),
                         "[Current time: Thursday 2026-12-24 10:12 CET (Europe/Berlin)]")

    def test_forwarded_message_carries_current_time_after_member_tag(self):
        self.post("/v1/assist", {"text": "What time is it?", "conversation_id": "c1", "user_id": "uLeon"})
        lines = self.fake.last.json["messages"][0]["content"].split("\n")
        self.assertEqual(lines[0], "[Household member: Leon]")
        self.assertTrue(lines[1].startswith("[Current time: ") and lines[1].endswith("(Europe/Berlin)]"))
        self.assertEqual(lines[2], "What time is it?")

    def test_unknown_or_missing_user_refused(self):
        for uid in ("uStranger", None, ""):
            code, body = self.post("/v1/assist", {"text": "hi", "conversation_id": "c1", "user_id": uid, "language": "de"})
            self.assertEqual(code, 403)
            self.assertIn("only answers Leon and Alina", body["reply"])
        self.assertIsNone(self.fake.last)

    def test_wrong_key_refused_without_detail(self):
        code, _ = self.post("/v1/assist", {"text": "hi", "conversation_id": "c1", "user_id": "uLeon"}, key="nope")
        self.assertEqual(code, 401)

    def test_session_id_stable_per_member_and_conversation(self):
        a, b, c = hi.session_id("uLeon", "c1"), hi.session_id("uLeon", "c1"), hi.session_id("uAlina", "c1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_hermes_down_is_plain_error(self):
        self.fake.stop()
        code, body = self.post("/v1/assist", {"text": "hi", "conversation_id": "c1", "user_id": "uLeon"})
        self.assertEqual(code, 502)
        self.assertIn("could not reach the household assistant", body["reply"])

    def test_oversized_text_refused(self):
        code, _ = self.post("/v1/assist", {"text": "x" * 20001, "conversation_id": "c1", "user_id": "uLeon"})
        self.assertEqual(code, 413)

    def test_members_file_reloaded_on_change(self):
        code, _ = self.post("/v1/assist", {"text": "hi", "conversation_id": "c", "user_id": "uAlina"})
        self.assertEqual(code, 403)
        time.sleep(0.01)
        with open(self.members_path, "w") as f:
            json.dump({"uLeon": "Leon", "uAlina": "Alina"}, f)
        code, _ = self.post("/v1/assist", {"text": "hi", "conversation_id": "c", "user_id": "uAlina"})
        self.assertEqual(code, 200)
        self.assertTrue(self.fake.last.json["messages"][0]["content"].startswith("[Household member: Alina]"))

    def test_health_needs_no_key(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            self.assertEqual(json.load(r), {"ok": True})

    def test_delegate_needs_delegate_key(self):
        body = {"card_id": "t1", "title": "Plan dinner", "body": "3 days"}
        self.assertEqual(self.post("/v1/delegate", body, key=ASSIST)[0], 401)
        self.assertEqual(self.post("/v1/assist", {"text": "hi", "user_id": "uLeon"}, key=DELEGATE)[0], 401)
        code, out = self.post("/v1/delegate", body, key=DELEGATE)
        self.assertEqual((code, out["status"]), (200, "done"))
        self.assertEqual(self.fake.last.headers["X-Hermes-Session-Id"], "card-t1")
        content = self.fake.last.json["messages"][0]["content"]
        head, clock, rest = content.split("\n", 2)
        self.assertEqual(head, "[Delegated task from Leon's personal assistant, card t1]")
        self.assertRegex(clock, r"^\[Current time: \w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2} \w+ \(Europe/Berlin\)\]$")
        self.assertEqual(rest, "Plan dinner\n\n3 days")

    def test_delegate_blocked_prefix(self):
        self.fake.reply = "BLOCKED: I cannot read mail."
        code, out = self.post("/v1/delegate", {"card_id": "t2", "title": "Read mail", "body": ""}, key=DELEGATE)
        self.assertEqual((code, out["status"]), (200, "blocked"))

    def test_delegate_unreachable_is_blocked(self):
        self.fake.stop()
        code, out = self.post("/v1/delegate", {"card_id": "t3", "title": "x", "body": ""}, key=DELEGATE)
        self.assertEqual((code, out["status"]), (502, "blocked"))

    JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 20).decode()
    PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()

    def test_assist_with_images_sends_multimodal(self):
        images = [{"name": "a.jpg", "data": self.JPEG}, {"name": "b.png", "data": self.PNG}]
        code, _ = self.post("/v1/assist", {"text": "Was ist das?", "user_id": "uLeon", "images": images})
        self.assertEqual(code, 200)
        content = self.fake.last.json["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[0]["text"].startswith("[Household member: Leon]\n[Current time: "))
        self.assertIn("[Photos attached: a.jpg, b.png]\nWas ist das?", content[0]["text"])
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + self.JPEG}})
        self.assertEqual(content[2]["image_url"]["url"], "data:image/png;base64," + self.PNG)

    def test_assist_image_limits(self):
        img = {"name": "a.jpg", "data": self.JPEG}
        bad = [
            (413, [img] * 4),
            (400, [{"name": "a.jpg", "data": "!!!notbase64"}]),
            (400, [{"name": "a.jpg", "data": base64.b64encode(b"GIF89a....").decode()}]),
            (400, [{"name": 5, "data": self.JPEG}]),
            (400, "nope"),
            (413, [{"name": "a.jpg", "data": "A" * (hi.MAX_IMAGE_B64 + 4)}]),
        ]
        for want, images in bad:
            code, _ = self.post("/v1/assist", {"text": "hi", "user_id": "uLeon", "images": images})
            self.assertEqual(code, want, images if want != 413 else want)
        self.assertIsNone(self.fake.last)

    def test_too_many_photos_message(self):
        code, out = self.post("/v1/assist", {"text": "hi", "user_id": "uLeon",
                                             "images": [{"name": "a.jpg", "data": self.JPEG}] * 4})
        self.assertEqual((code, out["reply"]), (413, "Höchstens 3 Fotos pro Frage."))

    def test_assist_body_over_limit_refused(self):
        old, hi.MAX_ASSIST_BODY = hi.MAX_ASSIST_BODY, 5000
        try:
            code, _ = self.post("/v1/assist", {"text": "hi", "user_id": "uLeon",
                                               "images": [{"name": "a.jpg", "data": "A" * 6000}]})
        finally:
            hi.MAX_ASSIST_BODY = old
        self.assertEqual(code, 413)
        self.assertIsNone(self.fake.last)

    def test_text_only_unchanged(self):
        self.post("/v1/assist", {"text": "hi", "user_id": "uLeon"})
        self.assertIsInstance(self.fake.last.json["messages"][0]["content"], str)

    def test_delegate_body_limit_unchanged(self):
        code, _ = self.post("/v1/delegate", {"card_id": "t", "title": "x", "body": "y" * 90000}, key=DELEGATE)
        self.assertEqual(code, 413)
        self.assertIsNone(self.fake.last)

    def test_log_line_counts_images(self):
        with self.assertLogs("household_ingress", "INFO") as cm:
            self.post("/v1/assist", {"text": "hi", "user_id": "uLeon", "images": [{"name": "a.jpg", "data": self.JPEG}]})
            self.post("/v1/assist", {"text": "hi", "user_id": "uLeon"})
        lines = [l for l in cm.output if "/v1/assist" in l]
        self.assertIn("images=1", lines[0])
        self.assertIn("images=0", lines[1])


if __name__ == "__main__":
    unittest.main()


class AdminRoleTest(unittest.TestCase):
    """Role file and single flight: the guard sees one role file, so an admin request runs alone."""

    def setUp(self):
        self.fake = FakeHermes()
        self.dir = tempfile.TemporaryDirectory()
        members = os.path.join(self.dir.name, "members.json")
        with open(members, "w") as f:
            json.dump({"uLeon": "Leon", "uAlina": "Alina"}, f)
        self.role = os.path.join(self.dir.name, "role.json")
        os.environ.update(ASSIST_KEY=ASSIST, API_SERVER_KEY=API, DELEGATE_KEY=DELEGATE, ADMIN_IDS="uLeon",
                          ROLE_FILE=self.role, HERMES_URL=f"http://127.0.0.1:{self.fake.port}", MEMBERS_FILE=members)
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):  # records the role file at the moment Hermes is called
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(n))
                try:
                    with open(outer.role) as f:
                        role = json.load(f)
                except (OSError, ValueError):
                    role = None
                outer.seen.append((body["messages"][0]["content"], role))
                if outer.hold:
                    outer.hold.wait(5)
                data = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.hold = None
        self.fake.stop()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        os.environ["HERMES_URL"] = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.cfg = hi.load_cfg()
        self.servers = hi.serve(self.cfg, ["127.0.0.1"], 0)
        self.port = self.servers[0].server_address[1]

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()
        os.environ.pop("ADMIN_IDS", None)
        os.environ.pop("ROLE_FILE", None)

    def ask(self, user, text="hi"):
        return post(self.port, "/v1/assist", {"text": text, "conversation_id": "c", "user_id": user}, ASSIST)

    def test_admin_request_writes_role_file_then_removes_it(self):
        code, _ = self.ask("uLeon")
        self.assertEqual(code, 200)
        content, role = self.seen[0]
        self.assertEqual(role["role"], "admin")
        self.assertTrue(0 < role["expires"] - time.time() <= hi.ASSIST_TIMEOUT + hi.ROLE_MARGIN)
        self.assertIn("[Access: admin.", content)
        self.assertFalse(os.path.exists(self.role))
        self.assertEqual(os.stat(self.dir.name).st_mode & 0o022, 0)

    def test_other_member_never_gets_role_file_or_line(self):
        self.ask("uAlina")
        content, role = self.seen[0]
        self.assertIsNone(role)
        self.assertNotIn("[Access:", content)

    def test_role_file_removed_when_hermes_fails(self):
        self.srv.shutdown()
        self.srv.server_close()
        code, _ = self.ask("uLeon")
        self.assertEqual(code, 502)
        self.assertFalse(os.path.exists(self.role))
        self.assertEqual(self.cfg["flight"]._active, 0)

    def test_nobody_is_admin_without_admin_ids(self):
        os.environ["ADMIN_IDS"] = ""
        cfg = hi.load_cfg()
        self.assertIsNone(cfg["roles"].path)
        self.assertEqual(cfg["admin_ids"], set())

    def test_stale_role_file_removed_at_start(self):
        with open(self.role, "w") as f:
            f.write("{}")
        hi.load_cfg()
        self.assertFalse(os.path.exists(self.role))

    def test_admin_runs_alone_other_member_waits(self):
        self.hold = threading.Event()
        results = {}
        t1 = threading.Thread(target=lambda: results.setdefault("leon", self.ask("uLeon")))
        t1.start()
        deadline = time.time() + 5
        while not self.seen and time.time() < deadline:
            time.sleep(0.01)
        t2 = threading.Thread(target=lambda: results.setdefault("alina", self.ask("uAlina")))
        t2.start()
        time.sleep(0.3)
        self.assertEqual(len(self.seen), 1)       # Alina's request has not reached Hermes while Leon's runs
        self.hold.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual([c for c, _ in self.seen if "Alina" in c and "[Access:" in c], [])
        self.assertEqual(self.seen[1][1], None)   # Alina ran after the file was removed
        self.assertEqual(results["leon"][0], 200)
        self.assertEqual(results["alina"][0], 200)

    def test_admin_downgraded_while_another_request_runs(self):
        flight = hi.Flight()
        self.assertEqual(flight.enter(False), "member")
        self.assertEqual(flight.enter(True, admin_wait=0.05), "member")   # could not run alone
        flight.leave("member")
        flight.leave("member")
        self.assertEqual(flight.enter(True), "admin")
        flight.leave("admin")

    def test_member_gets_busy_when_admin_does_not_finish(self):
        flight = hi.Flight()
        self.assertEqual(flight.enter(True), "admin")
        self.assertIsNone(flight.enter(False, member_wait=0.05))
        flight.leave("admin")

    def test_delegation_waits_for_admin_and_never_runs_as_admin(self):
        flight = self.cfg["flight"]
        self.assertEqual(flight.enter(True), "admin")
        self.cfg["delegate_wait"] = 0.05
        code, out, _ = hi.handle_delegate(self.cfg, {"card_id": "c1", "title": "t"})
        self.assertEqual(code, 503)
        flight.leave("admin")
        code, out, _ = hi.handle_delegate(self.cfg, {"card_id": "c1", "title": "t"})
        self.assertEqual(code, 200)
        self.assertIsNone(self.seen[-1][1])

    def test_unwritable_role_dir_runs_as_member(self):
        self.cfg["roles"].path = os.path.join(self.dir.name, "missing-dir", "role.json")
        code, _ = self.ask("uLeon")
        self.assertEqual(code, 200)
        content, role = self.seen[0]
        self.assertNotIn("[Access: admin", content)
        self.assertIn("[Access: standard.", content)


class QuarantineTest(unittest.TestCase):
    """A request that ends without a reply may still be running inside Hermes: no admin until the quarantine passes."""

    def test_no_admin_during_quarantine_then_admin_again(self):
        now = [100.0]
        flight = hi.Flight(clock=lambda: now[0])
        flight.taint(900)
        self.assertEqual(flight.enter(True, admin_wait=0.01), "member")
        flight.leave("member")
        now[0] += 899
        self.assertEqual(flight.enter(True, admin_wait=0.01), "member")
        flight.leave("member")
        now[0] += 2
        self.assertEqual(flight.enter(True), "admin")
        flight.leave("admin")

    def test_quarantine_set_while_admin_waits(self):
        now = [0.0]
        flight = hi.Flight(clock=lambda: now[0])
        self.assertEqual(flight.enter(False), "member")
        t = threading.Thread(target=lambda: flight.taint(900))
        t.start()
        t.join()
        flight.leave("member")                       # the failed request is gone, but its run may not be
        self.assertEqual(flight.enter(True, admin_wait=0.01), "member")

    def test_taint_never_shortens(self):
        now = [0.0]
        flight = hi.Flight(clock=lambda: now[0])
        flight.taint(900)
        flight.taint(10)
        now[0] = 500
        self.assertEqual(flight.enter(True, admin_wait=0.01), "member")

    def test_failed_requests_start_the_quarantine(self):
        # a Hermes that is down, for a member and for an admin request, and a failed delegation
        d = tempfile.TemporaryDirectory()
        members = os.path.join(d.name, "m.json")
        with open(members, "w") as f:
            json.dump({"uLeon": "Leon", "uAlina": "Alina"}, f)
        os.environ.update(ASSIST_KEY=ASSIST, API_SERVER_KEY=API, DELEGATE_KEY=DELEGATE, ADMIN_IDS="uLeon",
                          ROLE_FILE=os.path.join(d.name, "role.json"), MEMBERS_FILE=members, HERMES_URL="http://127.0.0.1:9")
        try:
            for who in ("uAlina", "uLeon"):
                cfg = hi.load_cfg()
                code, _, _ = hi.handle_assist(cfg, {"text": "hi", "user_id": who})
                self.assertEqual(code, 502)
                self.assertEqual(cfg["flight"].enter(True, admin_wait=0.01), "member", who)
            cfg = hi.load_cfg()
            code, _, _ = hi.handle_delegate(cfg, {"card_id": "c", "title": "t"})
            self.assertEqual(code, 502)
            self.assertEqual(cfg["flight"].enter(True, admin_wait=0.01), "member")
        finally:
            os.environ.pop("ADMIN_IDS", None)
            os.environ.pop("ROLE_FILE", None)
            d.cleanup()
