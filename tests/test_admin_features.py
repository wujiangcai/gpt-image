import importlib
import errno
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
                "AUTH_FILE": str(self.auth_path),
                "MODE": "relay",
                "IMAGE_API_BASE": "https://relay.example/v1/images",
                "IMAGE_API_KEY": "relay-key",
                "CHAT_API_KEY": "chat-key",
                "C2A_BASE": "http://c2a.test",
                "C2A_KEY": "c2a-key",
                "CHAT_IMAGE_HOST_ALLOWLIST": "",
                "MAX_CHAT_IMAGE_BYTES": str(20 * 1024 * 1024),
                "MAX_CONCURRENT_IMAGE_REQUESTS": "3",
                "USER_RATE_LIMIT_PER_MINUTE": "30",
            },
            clear=False,
        )
        self.env.start()

        import main

        self.main = importlib.reload(main)
        self.main._save_auth({"admin_token": "admin-test-token", "users": []})
        self.main._recent_usage.clear()
        self.main.CHAT_IMAGE_HOST_ALLOWLIST.clear()
        self.client = TestClient(self.main.app)
        self.headers = {"Authorization": "Bearer admin-test-token"}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _set_runtime_mode(self, mode):
        data = self.main._load_auth()
        settings = data.get("settings") or {}
        settings["mode"] = mode
        data["settings"] = settings
        self.main._save_auth(data)

    def test_save_auth_falls_back_for_bind_mount_replace_failure(self):
        self.auth_path.write_text(json.dumps({"admin_token": "old", "users": []}), "utf-8")
        os.chmod(self.auth_path, 0o600)

        def fail_replace(tmp):
            raise OSError(errno.EBUSY, "busy bind mount")

        with patch.object(self.main, "_replace_auth_file", fail_replace):
            self.main._save_auth({"admin_token": "new", "users": [{"name": "u"}]})

        data = json.loads(self.auth_path.read_text("utf-8"))
        self.assertEqual(data["admin_token"], "new")
        self.assertEqual(data["users"][0]["name"], "u")
        if os.name != "nt":
            self.assertEqual(self.auth_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(Path(self.tmp.name).glob("_auth.json.*.tmp")), [])

    def test_mode_settings_persist_and_health_uses_runtime_mode(self):
        r = self.client.put("/api/settings/mode", json={"mode": "chat2api"}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")

        r = self.client.get("/api/settings/mode", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")

        r = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "chat2api")
        self.assertEqual(r.json()["chat_base"], self.main.CHAT_BASE)

    def test_health_requires_auth_and_public_livez_is_minimal(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 401)

        r = self.client.get("/livez")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

        r = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertNotIn("proxy", data)

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

    def test_delete_accounts_treats_5xx_as_failure(self):
        async def fake_request(method, path, *, json_body=None):
            return 500, {"error": "server error", "tokens": json_body["tokens"]}

        token = "eyJhbGciOiJSUzI1NiIs.test-token-value"
        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.post("/api/accounts/remove", json={"tokens": [token]}, headers=self.headers)

        self.assertEqual(r.status_code, 502)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["deleted"], 0)
        self.assertNotIn(token, json.dumps(data, ensure_ascii=False))

    def test_accounts_list_redacts_tokens_and_remove_accepts_mask(self):
        token = "eyJhbGciOiJSUzI1NiIs.real-account-token"
        calls = []

        async def fake_request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return 200, {"items": [{"email": "a@example.com", "access_token": token, "refresh_token": "refresh-secret-token"}]}
            return 200, {"ok": True, "deleted": len(json_body["tokens"]), "tokens": json_body["tokens"]}

        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.get("/api/accounts", headers=self.headers)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertNotIn(token, json.dumps(data, ensure_ascii=False))
            masked = data["items"][0]["access_token"]
            self.assertIn("…", masked)

            r = self.client.post("/api/accounts/remove", json={"tokens": [masked]}, headers=self.headers)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[-1][2]["tokens"], [token])

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

    def test_generate_chat2api_requests_once_per_image_and_returns_images(self):
        self._set_runtime_mode("chat2api")
        posts = []
        gets = []
        self.main.CHAT_IMAGE_HOST_ALLOWLIST.add("cdn.example")

        class FakeResponse:
            def __init__(self, status_code=200, body=None, content=b""):
                self.status_code = status_code
                self._body = body or {}
                self.content = content
                self.text = json.dumps(self._body)
                self.headers = {"content-type": "image/png", "content-length": str(len(content))}

            def json(self):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_bytes(self):
                yield self.content

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                idx = len(posts) + 1
                posts.append({"url": url, "json": json, "headers": headers})
                return FakeResponse(
                    body={
                        "choices": [
                            {
                                "message": {
                                    "content": f"![generated](https://cdn.example/image-{idx}.png)"
                                }
                            }
                        ]
                    }
                )

            def stream(self, method, url, follow_redirects=False):
                gets.append(url)
                return FakeResponse(content=f"image-bytes-{len(gets)}".encode())

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            with patch.object(self.main.socket, "getaddrinfo", return_value=[(self.main.socket.AF_INET, self.main.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
                r = self.client.post(
                    "/api/generate",
                    json={"prompt": "test image", "size": "1024x1024", "n": 3},
                    headers=self.headers,
                )

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(posts), 3)
        self.assertEqual(len(gets), 3)
        self.assertEqual(len(data["data"]), 3)
        self.assertEqual([p["url"] for p in posts], [f"{self.main.CHAT_BASE}/chat/completions"] * 3)
        self.assertEqual(
            [p["json"]["messages"][0]["content"] for p in posts],
            [
                "test image\n\n(Image size: 1024x1024)\n\n(Batch image 1 of 3; create one image.)",
                "test image\n\n(Image size: 1024x1024)\n\n(Batch image 2 of 3; create one image.)",
                "test image\n\n(Image size: 1024x1024)\n\n(Batch image 3 of 3; create one image.)",
            ],
        )
        self.assertEqual([item["b64_json"] for item in data["data"]], ["aW1hZ2UtYnl0ZXMtMQ==", "aW1hZ2UtYnl0ZXMtMg==", "aW1hZ2UtYnl0ZXMtMw=="])

    def test_generate_chat2api_partial_failure_returns_data_error_item(self):
        self._set_runtime_mode("chat2api")
        posts = []
        gets = []
        self.main.CHAT_IMAGE_HOST_ALLOWLIST.add("cdn.example")
        chat_responses = [
            {"status": 200, "body": {"choices": [{"message": {"content": "![one](https://cdn.example/one.png)"}}]}},
            {"status": 503, "body": {"error": {"message": "temporarily unavailable"}}},
            {"status": 200, "body": {"choices": [{"message": {"content": "![two](https://cdn.example/two.png)"}}]}},
        ]

        class FakeResponse:
            def __init__(self, status_code=200, body=None, content=b""):
                self.status_code = status_code
                self._body = body or {}
                self.content = content
                self.text = json.dumps(self._body)
                self.headers = {"content-type": "image/png", "content-length": str(len(content))}

            def json(self):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def aiter_bytes(self):
                yield self.content

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                idx = len(posts)
                posts.append({"url": url, "json": json})
                resp = chat_responses[idx]
                return FakeResponse(status_code=resp["status"], body=resp["body"])

            def stream(self, method, url, follow_redirects=False):
                gets.append(url)
                return FakeResponse(content=f"download-{len(gets)}".encode())

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            with patch.object(self.main.socket, "getaddrinfo", return_value=[(self.main.socket.AF_INET, self.main.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
                r = self.client.post(
                    "/api/generate",
                    json={"prompt": "test image", "size": "1024x1024", "n": 3},
                    headers=self.headers,
                )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        image_items = [item for item in data if "b64_json" in item]
        error_items = [item for item in data if "error" in item]
        self.assertEqual(len(posts), 3)
        self.assertEqual(len(gets), 2)
        self.assertEqual(len(image_items), 2)
        self.assertEqual(len(error_items), 1)
        self.assertEqual(error_items[0]["error"], {"error": {"message": "temporarily unavailable"}})

    def test_generate_chat2api_blocks_private_image_urls(self):
        self._set_runtime_mode("chat2api")

        class FakeResponse:
            def __init__(self, status_code=200, body=None):
                self.status_code = status_code
                self._body = body or {}
                self.text = json.dumps(self._body)

            def json(self):
                return self._body

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                return FakeResponse(body={"choices": [{"message": {"content": "![bad](http://127.0.0.1/secret.png)"}}]})

            def stream(self, method, url, follow_redirects=False):
                raise AssertionError("private URL should not be fetched")

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "test image", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertIn("blocked address", r.json()["data"][0]["error"])

    def test_generate_rate_limit_returns_429(self):
        self.main.USER_RATE_LIMIT_PER_MINUTE = 1

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
                "/api/generate",
                json={"prompt": "one", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)

            r = self.client.post(
                "/api/generate",
                json={"prompt": "two", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers.get("retry-after"), "60")

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

    def test_edits_forwards_quality_with_reference_image(self):
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
                captured["url"] = url
                captured["data"] = data
                captured["files"] = files
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "edit this", "size": "1024x1024", "n": "1", "quality": "medium"},
                files={"image": ("ref.png", b"png-bytes", "image/png")},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["url"], "https://relay.example/v1/images/edits")
        self.assertEqual(captured["data"]["quality"], "medium")
        self.assertIn("image", captured["files"])

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
