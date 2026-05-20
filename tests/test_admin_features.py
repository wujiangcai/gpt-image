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


if __name__ == "__main__":
    unittest.main()
