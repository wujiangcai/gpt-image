import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class AdminFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_path = Path(self.tmp.name) / "_auth.json"
        self.env = patch.dict(
            os.environ,
            {
                "ADMIN_TOKEN": "admin-test-token",
                "MODE": "relay",
                "IMAGE_API_BASE": "https://relay.example/v1/images",
                "IMAGE_API_KEY": "relay-key",
                "CHAT_API_KEY": "chat-key",
                "C2A_BASE": "http://c2a.test",
                "C2A_KEY": "c2a-key",
            },
            clear=False,
        )
        self.env.start()

        import main

        self.main = importlib.reload(main)
        self.main._AUTH_FILE = self.auth_path
        self.main._save_auth({"admin_token": "admin-test-token", "users": []})
        self.client = TestClient(self.main.app)
        self.headers = {"Authorization": "Bearer admin-test-token"}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_mode_settings_persist_and_health_uses_runtime_mode(self):
        r = self.client.put("/api/settings/mode", json={"mode": "chat2api"}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")

        r = self.client.get("/api/settings/mode", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")

        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")
        self.assertEqual(r.json()["chat_base"], self.main.CHAT_BASE)

    def test_delete_accounts_falls_back_and_masks_tokens(self):
        calls = []

        async def fake_request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            if method == "DELETE":
                return 405, {"error": "method not allowed", "tokens": json_body["tokens"]}
            return 200, {"ok": True, "deleted": 1, "tokens": json_body["tokens"]}

        token = "eyJhbGciOiJSUzI1NiIs.test-token-value"
        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.post("/api/accounts/remove", json={"tokens": [token]}, headers=self.headers)

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted"], 1)
        self.assertEqual(calls[0][0], "DELETE")
        self.assertEqual(calls[1][:2], ("POST", "/api/accounts/remove"))
        self.assertNotIn(token, json.dumps(data, ensure_ascii=False))
        self.assertIn("…", data["tokens"][0])

    def test_cleanup_preview_and_execute_abnormal_accounts(self):
        accounts = {
            "items": [
                {"email": "ok@example.com", "status": "normal", "quota": 4, "access_token": "normal-token-1234567890"},
                {"email": "bad@example.com", "status": "异常", "quota": 3, "access_token": "bad-token-1234567890"},
                {"email": "zero@example.com", "status": "normal", "quota": 0, "access_token": "zero-token-1234567890"},
            ]
        }
        deleted = []

        async def fake_request(method, path, *, json_body=None):
            if method == "GET":
                return 200, accounts
            deleted.extend(json_body["tokens"])
            return 200, {"ok": True, "deleted": len(json_body["tokens"])}

        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.post("/api/accounts/cleanup", json={"dry_run": True}, headers=self.headers)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["items"][0]["email"], "bad@example.com")
            self.assertNotIn("bad-token-1234567890", json.dumps(data, ensure_ascii=False))

            r = self.client.post(
                "/api/accounts/cleanup",
                json={"dry_run": False, "zero_quota": True},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["count"], 2)
            self.assertEqual(set(deleted), {"bad-token-1234567890", "zero-token-1234567890"})
            self.assertNotIn("zero-token-1234567890", json.dumps(data, ensure_ascii=False))
    def test_generate_maps_upstream_auth_failure_without_logging_out_user(self):
        class FakeResponse:
            status_code = 401
            text = '{"error":{"message":"bad upstream key"}}'

            def json(self):
                return {"error": {"message": "bad upstream key"}}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "test image", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["upstream_status"], 401)

    def test_edits_validates_upload_and_sends_single_image_field(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"abc"}]}'

            def json(self):
                return {"data": [{"b64_json": "abc"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                captured["files"] = files
                captured["data"] = data
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "edit this", "size": "1024x1024", "n": "2", "quality": "high"},
                files={"image": ("ref.png", b"png-bytes", "image/png")},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertIn("image", captured["files"])
        self.assertNotIn("image[]", captured["files"])
        self.assertEqual(captured["data"]["n"], "2")
        self.assertEqual(captured["data"]["quality"], "high")

        r = self.client.post(
            "/api/edits",
            data={"prompt": "edit this", "size": "1024x1024", "n": "1"},
            files={"image": ("ref.txt", b"not an image", "text/plain")},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_generate_relay_forwards_count_and_quality(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"abc"},{"b64_json":"def"}]}'

            def json(self):
                return {"data": [{"b64_json": "abc"}, {"b64_json": "def"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "test image", "size": "1536x1024", "n": 4, "quality": "high"},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["url"], "https://relay.example/v1/images/generations")
        self.assertEqual(captured["json"]["n"], 4)
        self.assertEqual(captured["json"]["quality"], "high")
        self.assertEqual(captured["json"]["size"], "1536x1024")

    def test_edits_accepts_generic_upload_content_type(self):
        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"abc"}]}'

            def json(self):
                return {"data": [{"b64_json": "abc"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "edit this", "size": "1024x1024", "n": "1"},
                files={"image": ("ref.png", b"png-bytes", "application/octet-stream")},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
