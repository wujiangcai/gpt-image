import asyncio
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
        self.main._usage_stats.update({"requests": 0, "requested_images": 0, "successful_images": 0, "failed_images": 0, "users": {}, "recent": []})
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

    def test_health_returns_model_routing_options(self):
        r = self.client.get("/api/health", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["routing"], "model")
        ids = [item["id"] for item in data["models"]]
        self.assertEqual(len(ids), len(set(ids)))
        models = {item["id"]: item for item in data["models"]}
        self.assertIn("relay:gpt-image-2", models)
        self.assertEqual(models["relay:gpt-image-2"]["model"], "gpt-image-2")
        self.assertEqual(models["relay:gpt-image-2"]["source"], "relay")
        self.assertTrue(models["relay:gpt-image-2"]["supports_edits"])
        self.assertIn("chat2api:gpt-image-2", models)
        self.assertEqual(models["chat2api:gpt-image-2"]["model"], "gpt-image-2")
        self.assertEqual(models["chat2api:gpt-image-2"]["source"], "chat2api")
        self.assertTrue(models["chat2api:gpt-image-2"]["supports_edits"])
        self.assertIn("chat2api:gpt-4o-image", models)
        self.assertEqual(models["chat2api:gpt-4o-image"]["source"], "chat2api")
        self.assertTrue(models["chat2api:gpt-4o-image"]["supports_edits"])

    def test_generate_routes_by_selected_model(self):
        calls = []

        async def fake_relay(req, model):
            calls.append(("relay", model, req.prompt))
            return {"data": [{"b64_json": "relay-image"}], "model": model}

        async def fake_chat2api(req, model):
            calls.append(("chat2api", model, req.prompt))
            return {"data": [{"b64_json": "pool-image"}], "model": model}

        with patch.object(self.main, "generate_via_relay", fake_relay), patch.object(self.main, "generate_via_chat2api", fake_chat2api):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "legacy relay image", "model": "gpt-image-2"},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.post(
                "/api/generate",
                json={"prompt": "explicit pool image", "model": "gpt-image-2", "source": "chat2api"},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.post(
                "/api/generate",
                json={"prompt": "option id pool image", "model": "chat2api:gpt-image-2"},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.post(
                "/api/generate",
                json={"prompt": "explicit relay image", "model": "gpt-image-2", "source": "relay"},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)
            r = self.client.post(
                "/api/generate",
                json={"prompt": "pool image", "model": "gpt-4o-image"},
                headers=self.headers,
            )
            self.assertEqual(r.status_code, 200)

        self.assertEqual(
            calls,
            [
                ("relay", "gpt-image-2", "legacy relay image"),
                ("chat2api", "gpt-image-2", "explicit pool image"),
                ("chat2api", "gpt-image-2", "option id pool image"),
                ("relay", "gpt-image-2", "explicit relay image"),
                ("chat2api", "gpt-4o-image", "pool image"),
            ],
        )

    def test_generate_rejects_unknown_model(self):
        r = self.client.post(
            "/api/generate",
            json={"prompt": "test image", "model": "unknown-image-model"},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("不支持的模型", r.json()["detail"])

    def test_edits_forward_to_chat2api_for_account_pool_source(self):
        posts = []

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"edited"}]}'

            def json(self):
                return {"data": [{"b64_json": "edited"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                posts.append({"url": url, "headers": headers, "data": data, "files": files})
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "edit this", "size": "1024x1024", "n": "1", "model": "gpt-image-2", "source": "chat2api"},
                files={"image": ("ref.png", b"png-bytes", "image/png")},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"][0]["b64_json"], "edited")
        self.assertEqual(posts[0]["url"], f"{self.main.CHAT_BASE}/images/edits")
        self.assertEqual(posts[0]["headers"]["Authorization"], f"Bearer {self.main.CHAT_KEY}")
        self.assertEqual(posts[0]["data"]["model"], "gpt-image-2")
        self.assertEqual(posts[0]["data"]["prompt"], "edit this")
        self.assertEqual(posts[0]["data"]["size"], "1024x1024")
        self.assertEqual(posts[0]["data"]["n"], "1")
        self.assertEqual(posts[0]["data"]["response_format"], "b64_json")
        image_parts = [item for item in posts[0]["files"] if item[0] == "image"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual(image_parts[0][1][0], "ref.png")

    def test_edits_forwards_multiple_images(self):
        posts = []

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"merged"}]}'

            def json(self):
                return {"data": [{"b64_json": "merged"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                posts.append({"url": url, "headers": headers, "data": data, "files": files})
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "merge these", "size": "1024x1024", "n": "1", "model": "gpt-image-2", "source": "chat2api"},
                files=[
                    ("image", ("a.png", b"png-a", "image/png")),
                    ("image", ("b.png", b"png-b", "image/png")),
                ],
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        image_parts = [item for item in posts[0]["files"] if item[0] == "image"]
        self.assertEqual(len(image_parts), 2)
        self.assertEqual(image_parts[0][1][0], "a.png")
        self.assertEqual(image_parts[1][1][0], "b.png")

    def test_edits_rejects_too_many_images(self):
        r = self.client.post(
            "/api/edits",
            data={"prompt": "too many", "size": "1024x1024", "n": "1", "model": "gpt-image-2", "source": "chat2api"},
            files=[("image", (f"ref{i}.png", b"png-bytes", "image/png")) for i in range(5)],
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("参考图最多", r.json()["detail"])

    def test_edits_forward_to_relay_source(self):
        posts = []

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"relay-edited"}]}'

            def json(self):
                return {"data": [{"b64_json": "relay-edited"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                posts.append({"url": url, "headers": headers, "data": data, "files": files})
                return FakeResponse()

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/edits",
                data={"prompt": "edit relay", "size": "1024x1024", "n": "1", "model": "gpt-image-2", "source": "relay"},
                files={"image": ("ref.png", b"png-bytes", "image/png")},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(posts[0]["url"], "https://relay.example/v1/images/edits")
        self.assertEqual(posts[0]["headers"]["Authorization"], "Bearer relay-key")
        self.assertEqual(posts[0]["data"]["model"], "gpt-image-2")
        self.assertNotIn("response_format", posts[0]["data"])

    def test_edits_chat2api_requires_chat_key(self):
        self.main.CHAT_KEY = ""
        r = self.client.post(
            "/api/edits",
            data={"prompt": "edit this", "size": "1024x1024", "n": "1", "model": "gpt-image-2", "source": "chat2api"},
            files={"image": ("ref.png", b"png-bytes", "image/png")},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 500)
        self.assertIn("账号池生图 key", r.json()["detail"])

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

    def test_accounts_list_redacts_tokens_and_remove_accepts_token_id(self):
        token = "eyJhbGciOiJSUzI1NiIs.real-account-token"
        refresh = "refresh-secret-token"
        calls = []

        async def fake_request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return 200, {"items": [{"email": "a@example.com", "access_token": token, "refresh_token": refresh}]}
            return 200, {"ok": True, "deleted": len(json_body["tokens"]), "tokens": json_body["tokens"]}

        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.get("/api/accounts", headers=self.headers)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            dumped = json.dumps(data, ensure_ascii=False)
            self.assertNotIn(token, dumped)
            self.assertNotIn(refresh, dumped)
            item = data["items"][0]
            self.assertIn("…", item["access_token"])
            self.assertIn("…", item["token_masked"])
            self.assertRegex(item["token_id"], r"^[0-9a-f]{16}$")

            r = self.client.post("/api/accounts/remove", json={"token_ids": [item["token_id"]]}, headers=self.headers)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[-1][2]["tokens"], [token])

    def test_accounts_refresh_accepts_token_id_and_redacts_response(self):
        token = "eyJhbGciOiJSUzI1NiIs.refresh-account-token"
        calls = []

        async def fake_request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return 200, {"items": [{"email": "a@example.com", "access_token": token}]}
            return 200, {"ok": True, "access_tokens": json_body["access_tokens"], "access_token": token}

        token_id = self.main._token_id(token)
        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.post("/api/accounts/refresh", json={"token_ids": [token_id]}, headers=self.headers)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[-1][2]["access_tokens"], [token])
        self.assertNotIn(token, json.dumps(r.json(), ensure_ascii=False))

    def test_accounts_update_accepts_token_id_and_redacts_response(self):
        token = "eyJhbGciOiJSUzI1NiIs.update-account-token"
        calls = []

        async def fake_request(method, path, *, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return 200, {"items": [{"email": "a@example.com", "access_token": token, "disabled": False}]}
            return 200, {"item": {"email": "a@example.com", "access_token": token, "disabled": json_body["disabled"]}}

        token_id = self.main._token_id(token)
        with patch.object(self.main, "_c2a_raw_request", fake_request):
            r = self.client.post(
                "/api/accounts/update",
                json={"token_id": token_id, "disabled": True, "reset_consecutive_fail": True},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls[-1][:2], ("POST", "/api/accounts/update"))
        self.assertEqual(calls[-1][2]["access_token"], token)
        self.assertTrue(calls[-1][2]["disabled"])
        self.assertTrue(calls[-1][2]["reset_consecutive_fail"])
        self.assertNotIn(token, json.dumps(r.json(), ensure_ascii=False))


        accounts = {
            "items": [
                {"email": "ok@example.com", "status": "normal", "quota": 4, "access_token": "normal-token-1234567890"},
                {"email": "bad@example.com", "status": "异常", "quota": 3, "access_token": "bad-token-1234567890"},
                {"email": "zero@example.com", "status": "normal", "quota": 0, "access_token": "zero-token-1234567890"},
                {"email": "disabled@example.com", "status": "异常", "quota": 0, "disabled": True, "access_token": "disabled-token-1234567890"},
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
            self.assertNotIn("disabled-token-1234567890", set(deleted))
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
                posts.append({"url": url, "json": json, "headers": headers})
                return FakeResponse(body={"data": [{"b64_json": "one"}, {"b64_json": "two"}, {"b64_json": "three"}]})

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
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(gets), 0)
        self.assertEqual(len(data["data"]), 3)
        self.assertEqual(posts[0]["url"], f"{self.main.CHAT_BASE}/images/generations")
        self.assertEqual(posts[0]["json"]["prompt"], "test image")
        self.assertEqual(posts[0]["json"]["n"], 3)
        self.assertEqual(posts[0]["json"]["size"], "1024x1024")
        self.assertEqual([item["b64_json"] for item in data["data"]], ["one", "two", "three"])

    def test_generate_chat2api_partial_failure_returns_data_error_item(self):
        self._set_runtime_mode("chat2api")
        posts = []
        gets = []
        self.main.CHAT_IMAGE_HOST_ALLOWLIST.add("cdn.example")
        image_response = {"data": [{"b64_json": "one"}, {"error": {"message": "temporarily unavailable"}}, {"b64_json": "two"}]}

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
                posts.append({"url": url, "json": json})
                return FakeResponse(status_code=200, body=image_response)

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
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(gets), 0)
        self.assertEqual(len(image_items), 2)
        self.assertEqual(len(error_items), 1)
        self.assertEqual(error_items[0]["error"], {"message": "temporarily unavailable"})

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
                return FakeResponse(status_code=502, body={"error": {"message": "blocked address"}})

            def stream(self, method, url, follow_redirects=False):
                raise AssertionError("private URL should not be fetched")

        with patch.object(self.main.httpx, "AsyncClient", FakeClient):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "test image", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 502)
        self.assertIn("blocked address", r.json()["error"]["error"]["message"])

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

    def test_usage_requires_admin_and_records_generate(self):
        r = self.client.get("/api/usage")
        self.assertEqual(r.status_code, 401)

        class FakeResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"abc"},{"error":"upstream partial"}]}'

            def json(self):
                return {"data": [{"b64_json": "abc"}, {"error": "upstream partial"}]}

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
                json={"prompt": "usage test", "size": "1024x1024", "n": 2},
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 200)

        r = self.client.get("/api/usage", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["requests"], 1)
        self.assertEqual(data["requested_images"], 2)
        self.assertEqual(data["successful_images"], 1)
        self.assertEqual(data["failed_images"], 1)
        self.assertEqual(data["recent"][0]["endpoint"], "generate")


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
        image_parts = [item for item in captured["files"] if item[0] == "image"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual(image_parts[0][1][0], "ref.png")
        self.assertNotIn("image[]", [item[0] for item in captured["files"]])
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
        image_parts = [item for item in captured["files"] if item[0] == "image"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual(image_parts[0][1][0], "ref.png")

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

    def test_generate_cancels_on_client_disconnect(self):
        posts = []

        class SlowResponse:
            status_code = 200
            text = '{"data":[{"b64_json":"late"}]}'

            def json(self):
                return {"data": [{"b64_json": "late"}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                posts.append({"url": url, "json": json})
                # 模拟上游慢响应：协程被 cancel 时抛 CancelledError
                await asyncio.sleep(10)
                return SlowResponse()

        # 第一次 is_disconnected()（进入信号量后的前置检查）返回 False，让请求进入 _await_cancellable；
        # 之后轮询返回 True，触发取消。
        disconnect_state = {"count": 0}

        async def fake_is_disconnected(self):
            disconnect_state["count"] += 1
            return disconnect_state["count"] > 1

        with patch.object(self.main.httpx, "AsyncClient", FakeClient), \
             patch.object(self.main.Request, "is_disconnected", fake_is_disconnected):
            r = self.client.post(
                "/api/generate",
                json={"prompt": "slow image", "size": "1024x1024", "n": 1},
                headers=self.headers,
            )

        self.assertEqual(r.status_code, 499)
        self.assertIn("客户端已取消", r.json()["detail"])
        # 取消不应计入 failed 统计
        usage = self.client.get("/api/usage", headers=self.headers).json()
        self.assertEqual(usage["failed_images"], 0)
        self.assertEqual(usage["successful_images"], 0)
        # 上游 post 已被触发（之后被 cancel 中断）
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
