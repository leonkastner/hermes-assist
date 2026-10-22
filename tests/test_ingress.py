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
        self.assertTrue(self.fake.last.json["messages"][0]["content"].startswith(
            "[Delegated task from Leon's personal assistant, card t1]\nPlan dinner\n\n3 days"))

    def test_delegate_blocked_prefix(self):
        self.fake.reply = "BLOCKED: I cannot read mail."
        code, out = self.post("/v1/delegate", {"card_id": "t2", "title": "Read mail", "body": ""}, key=DELEGATE)
        self.assertEqual((code, out["status"]), (200, "blocked"))

    def test_delegate_unreachable_is_blocked(self):
        self.fake.stop()
        code, out = self.post("/v1/delegate", {"card_id": "t3", "title": "x", "body": ""}, key=DELEGATE)
        self.assertEqual((code, out["status"]), (502, "blocked"))


if __name__ == "__main__":
    unittest.main()
