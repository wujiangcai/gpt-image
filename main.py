import base64
import asyncio
import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

MODE = os.getenv("MODE", "relay").lower()
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "180"))
MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
MAX_EDIT_IMAGE_BYTES = int(os.getenv("MAX_EDIT_IMAGE_BYTES", str(10 * 1024 * 1024)))
MAX_CHAT_IMAGE_BYTES = int(os.getenv("MAX_CHAT_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_CONCURRENT_IMAGE_REQUESTS = int(os.getenv("MAX_CONCURRENT_IMAGE_REQUESTS", "3"))
USER_RATE_LIMIT_PER_MINUTE = int(os.getenv("USER_RATE_LIMIT_PER_MINUTE", "30"))
EDIT_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
CHAT_IMAGE_HOST_ALLOWLIST = {
    host.strip().lower()
    for host in os.getenv("CHAT_IMAGE_HOST_ALLOWLIST", "").split(",")
    if host.strip()
}

# Relay mode (existing): forward OpenAI Images API to a real OpenAI-compatible endpoint
RELAY_BASE = os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1/images").rstrip("/")
RELAY_KEY = os.getenv("IMAGE_API_KEY", "")

# chat2api mode: translate /v1/images/generations -> /v1/chat/completions, parse markdown for image URLs
CHAT_BASE = os.getenv("CHAT_API_BASE", "http://127.0.0.1:3000/v1").rstrip("/")
CHAT_KEY = os.getenv("CHAT_API_KEY", "")
RELAY_ONLY_MODELS = {"gpt-image-2"}
DEFAULT_ACCOUNT_POOL_MODELS = ("gpt-4o-image", "gpt-4o")
PROXY = os.getenv("HTTP_PROXY") or os.getenv("PROXY") or None

# chatgpt2api account-management proxy (browser hits image-gen-demo, server forwards with hidden key)
C2A_BASE = os.getenv("C2A_BASE", "").rstrip("/")
C2A_KEY = os.getenv("C2A_KEY", "")

# ---------- auth (admin token + user keys) ----------

_AUTH_FILE = Path(os.getenv("AUTH_FILE", str(Path(__file__).parent / "_auth.json")))


def _gen_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def _load_auth() -> dict:
    if _AUTH_FILE.exists():
        try:
            return json.loads(_AUTH_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"admin_token": "", "users": []}


def _replace_auth_file(tmp: Path) -> None:
    tmp.replace(_AUTH_FILE)


def _save_auth(data: dict) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2)
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = _AUTH_FILE.stat().st_mode & 0o777
    except OSError:
        existing_mode = None
    tmp = _AUTH_FILE.with_name(f"{_AUTH_FILE.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(content, "utf-8")
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        _replace_auth_file(tmp)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if e.errno not in {errno.EBUSY, errno.EXDEV}:
            raise
        # Docker single-file bind mounts cannot always be replaced atomically.
        # Fall back to writing through the existing mount target.
        _AUTH_FILE.write_text(content, "utf-8")
        if existing_mode is not None:
            os.chmod(_AUTH_FILE, existing_mode)


def _bootstrap_auth() -> dict:
    data = _load_auth()
    env_admin = os.getenv("ADMIN_TOKEN", "").strip()
    if env_admin:
        if data.get("admin_token") != env_admin:
            data["admin_token"] = env_admin
            _save_auth(data)
    elif not data.get("admin_token"):
        data["admin_token"] = _gen_key("admin")
        _save_auth(data)
    return data


_auth_state = _bootstrap_auth()
_admin_token = _auth_state.get("admin_token", "")
_admin_token_display = f"{_admin_token[:8]}…{_admin_token[-6:]}" if len(_admin_token) > 16 else "***"
print(f"[auth] admin token loaded = {_admin_token_display}  (完整值见 AUTH_FILE 或 .env)")
print(f"[auth] {len(_auth_state.get('users', []))} user key(s) loaded from {_AUTH_FILE.name}")


def _extract_bearer(auth_header: str | None) -> str:
    if not auth_header:
        return ""
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _identity_for(token: str) -> dict | None:
    if not token:
        return None
    data = _load_auth()
    if token == data.get("admin_token"):
        return {"role": "admin", "name": "管理员", "id": "admin"}
    for u in data.get("users", []):
        if u.get("key") == token and u.get("enabled", True):
            u["last_used"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save_auth(data)
            return {"role": "user", "name": u.get("name", ""), "id": u.get("id", "")}
    return None


def require_user(authorization: str | None = Header(default=None)):
    ident = _identity_for(_extract_bearer(authorization))
    if not ident:
        raise HTTPException(401, "auth required")
    return ident


def require_admin(authorization: str | None = Header(default=None)):
    ident = require_user(authorization)
    if ident["role"] != "admin":
        raise HTTPException(403, "admin only")
    return ident


def _enforce_user_rate_limit(ident: dict, units: int = 1) -> None:
    if USER_RATE_LIMIT_PER_MINUTE <= 0:
        return
    now = time.time()
    key = str(ident.get("id") or ident.get("name") or "unknown")
    recent = [t for t in _recent_usage.get(key, []) if now - t < 60]
    if len(recent) + units > USER_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: max {USER_RATE_LIMIT_PER_MINUTE} image unit(s) per minute",
            headers={"Retry-After": "60"},
        )
    recent.extend([now] * max(units, 1))
    _recent_usage[key] = recent


def _image_result_counts(value) -> tuple[int, int]:
    content = value.body if isinstance(value, JSONResponse) else value
    if isinstance(content, (bytes, bytearray)):
        try:
            content = json.loads(content.decode("utf-8"))
        except Exception:
            content = None
    if not isinstance(content, dict):
        return 0, 1
    items = content.get("data")
    if not isinstance(items, list):
        return 0, 1 if content.get("error") else 0
    success = sum(1 for item in items if isinstance(item, dict) and (item.get("b64_json") or item.get("url")) and not item.get("error"))
    failed = sum(1 for item in items if isinstance(item, dict) and item.get("error"))
    return success, failed


def _short_error(value) -> str:
    content = value.body if isinstance(value, JSONResponse) else value
    if isinstance(content, (bytes, bytearray)):
        try:
            content = json.loads(content.decode("utf-8"))
        except Exception:
            return ""
    if not isinstance(content, dict):
        return ""
    err = content.get("error") or content.get("detail")
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or err
    text = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False) if err else ""
    return text[:240]


def _record_usage(ident: dict, endpoint: str, mode: str, requested: int, result, status_code: int, elapsed_ms: int) -> None:
    success, item_errors = _image_result_counts(result)
    failed = max(requested - success, item_errors, 0) if status_code < 400 else requested
    user_id = str(ident.get("id") or ident.get("name") or "unknown")
    user = _usage_stats["users"].setdefault(
        user_id,
        {"name": ident.get("name") or user_id, "role": ident.get("role", "user"), "requests": 0, "requested_images": 0, "successful_images": 0, "failed_images": 0},
    )
    _usage_stats["requests"] += 1
    _usage_stats["requested_images"] += requested
    _usage_stats["successful_images"] += success
    _usage_stats["failed_images"] += failed
    user["requests"] += 1
    user["requested_images"] += requested
    user["successful_images"] += success
    user["failed_images"] += failed
    event = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_id": user_id,
        "user_name": user["name"],
        "endpoint": endpoint,
        "mode": mode,
        "requested_images": requested,
        "successful_images": success,
        "failed_images": failed,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
    }
    error = _short_error(result)
    if error:
        event["error"] = error
    _usage_stats["recent"].insert(0, event)
    del _usage_stats["recent"][50:]


def _usage_snapshot() -> dict:
    users = sorted(_usage_stats["users"].items(), key=lambda kv: kv[1].get("requested_images", 0), reverse=True)
    return {
        "started_at": _usage_stats["started_at"],
        "requests": _usage_stats["requests"],
        "requested_images": _usage_stats["requested_images"],
        "successful_images": _usage_stats["successful_images"],
        "failed_images": _usage_stats["failed_images"],
        "users": [{"id": uid, **stats} for uid, stats in users],
        "recent": list(_usage_stats["recent"]),
    }

print(f"[ok] MODE={MODE}  MODEL={MODEL}")
if MODE == "chat2api":
    print(f"     chat completions endpoint = {CHAT_BASE}/chat/completions")
    print(f"     proxy for image download  = {PROXY or 'none'}")
else:
    print(f"     relay base = {RELAY_BASE}")

app = FastAPI(title="Image Gen Adapter")

_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_image_semaphore = asyncio.Semaphore(max(MAX_CONCURRENT_IMAGE_REQUESTS, 1))
_recent_usage: dict[str, list[float]] = {}
_usage_stats: dict = {
    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "requests": 0,
    "requested_images": 0,
    "successful_images": 0,
    "failed_images": 0,
    "users": {},
    "recent": [],
}


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"
    n: int = Field(default=1, ge=1, le=4)
    quality: Optional[str] = None
    model: Optional[str] = Field(default=None, max_length=120)


class RelaySettingsBody(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(default="gpt-image-2", min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=1000)
    clear_key: bool = False


class ModeSettingsBody(BaseModel):
    mode: Literal["relay", "chat2api"]


class CleanupAccountsBody(BaseModel):
    statuses: list[str] = Field(default_factory=list)
    zero_quota: bool = False
    dry_run: bool = False


def _normalize_mode(value: str) -> str:
    mode = (value or "relay").strip().lower()
    return mode if mode in {"relay", "chat2api"} else "relay"


def _current_mode() -> str:
    return _normalize_mode((_load_auth().get("settings") or {}).get("mode") or MODE)


def _account_pool_models() -> list[str]:
    configured = [m.strip() for m in os.getenv("ACCOUNT_POOL_MODELS", "").split(",") if m.strip()]
    models: list[str] = []
    for model in [*configured, *DEFAULT_ACCOUNT_POOL_MODELS, MODEL]:
        if model and model not in RELAY_ONLY_MODELS and model not in models:
            models.append(model)
    return models


def _model_options() -> list[dict]:
    relay = _public_relay_config()
    relay_ready = bool(relay["base_url"] and relay["key_loaded"])
    account_ready = bool(CHAT_KEY)
    options = [
        {
            "id": "gpt-image-2",
            "label": "GPT Image 2（中转站，支持参考图）",
            "source": "relay",
            "ready": relay_ready,
            "supports_edits": True,
        }
    ]
    for model in _account_pool_models():
        options.append(
            {
                "id": model,
                "label": f"{model}（账号池）",
                "source": "chat2api",
                "ready": account_ready,
                "supports_edits": False,
            }
        )
    return options


def _default_model() -> str:
    mode = _current_mode()
    if mode == "chat2api":
        models = _account_pool_models()
        return models[0] if models else MODEL
    return "gpt-image-2"


def _allowed_models() -> set[str]:
    return {item["id"] for item in _model_options()}


def _normalize_model(value: str | None) -> str:
    model = (value or "").strip() or _default_model()
    if model not in _allowed_models():
        raise HTTPException(400, f"不支持的模型：{model}")
    return model


def _source_for_model(model: str) -> Literal["relay", "chat2api"]:
    return "relay" if model in RELAY_ONLY_MODELS else "chat2api"


def _public_mode_config() -> dict:
    relay = _public_relay_config()
    return {
        "mode": _current_mode(),
        "relay_ready": bool(relay["base_url"] and relay["key_loaded"]),
        "account_pool_ready": bool(CHAT_KEY),
        "c2a_admin": bool(C2A_BASE and C2A_KEY),
    }


def _save_mode_config(body: ModeSettingsBody) -> dict:
    data = _load_auth()
    settings = data.get("settings") or {}
    settings["mode"] = body.mode
    data["settings"] = settings
    _save_auth(data)
    return _public_mode_config()


def _normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "API 路径必须以 http:// 或 https:// 开头")
    return url


def _relay_config() -> dict:
    relay = _load_auth().get("relay") or {}
    api_key = str(relay["api_key"]).strip() if "api_key" in relay else RELAY_KEY
    return {
        "base_url": str(relay.get("base_url") or RELAY_BASE).strip().rstrip("/"),
        "api_key": api_key,
        "model": str(relay.get("model") or MODEL).strip() or "gpt-image-2",
    }


def _public_relay_config() -> dict:
    cfg = _relay_config()
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "key_loaded": bool(cfg["api_key"]),
    }


def _save_relay_config(body: RelaySettingsBody) -> dict:
    data = _load_auth()
    current = data.get("relay") or {}
    relay = {
        "base_url": _normalize_url(body.base_url),
        "model": body.model.strip() or "gpt-image-2",
        "api_key": str(current.get("api_key") or RELAY_KEY).strip(),
    }
    if body.clear_key:
        relay["api_key"] = ""
    elif body.api_key.strip():
        relay["api_key"] = body.api_key.strip()
    data["relay"] = relay
    _save_auth(data)
    return _public_relay_config()


def _relay_auth_error() -> HTTPException:
    return HTTPException(500, "中转站 API key 未配置，请到管理员页面设置")


def _relay_url_error() -> HTTPException:
    return HTTPException(500, "中转站 API 路径未配置，请到管理员页面设置")


def _require_relay_config() -> dict:
    cfg = _relay_config()
    if not cfg["base_url"]:
        raise _relay_url_error()
    if not cfg["api_key"]:
        raise _relay_auth_error()
    return cfg


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:1000]}


def extract_image_urls(markdown: str) -> list:
    return _IMG_RE.findall(markdown or "")


def _host_matches_allowlist(host: str) -> bool:
    if not CHAT_IMAGE_HOST_ALLOWLIST:
        return True
    host = host.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in CHAT_IMAGE_HOST_ALLOWLIST)


def _address_is_blocked(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _validate_image_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("image URL must be http(s)")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("image URL host is required")
    if not _host_matches_allowlist(host):
        raise ValueError("image URL host is not allowed")
    try:
        if _address_is_blocked(host):
            raise ValueError("image URL resolves to a blocked address")
    except ValueError as e:
        if "blocked address" in str(e):
            raise
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"image URL host cannot be resolved: {e}") from e
    for info in infos:
        address = info[4][0]
        if _address_is_blocked(address):
            raise ValueError("image URL resolves to a blocked address")


async def _download_image_url(client: httpx.AsyncClient, url: str) -> dict:
    try:
        _validate_image_url(url)
    except ValueError as e:
        return {"url": url, "error": str(e)}

    try:
        async with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code != 200:
                return {"url": url, "error": f"download status {resp.status_code}"}
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in EDIT_IMAGE_TYPES:
                return {"url": url, "error": f"download content-type {content_type} is not supported"}
            length = resp.headers.get("content-length")
            if length and int(length) > MAX_CHAT_IMAGE_BYTES:
                return {"url": url, "error": f"download exceeds {MAX_CHAT_IMAGE_BYTES} bytes"}
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_CHAT_IMAGE_BYTES:
                    return {"url": url, "error": f"download exceeds {MAX_CHAT_IMAGE_BYTES} bytes"}
                chunks.append(chunk)
            b64 = base64.b64encode(b"".join(chunks)).decode()
            return {"b64_json": b64}
    except Exception as e:
        return {"url": url, "error": f"download exception: {e}"}


# ---------- relay mode (legacy, for otokapi / OpenAI official) ----------

def _upstream_error_status(status_code: int) -> int:
    # Keep local 401/403 reserved for this app's login state. If an upstream API
    # rejects its hidden key, the browser token is still valid and should not be cleared.
    return 502 if status_code in {401, 403} else status_code


async def generate_via_relay(req: GenerateRequest, model: str):
    if _source_for_model(model) != "relay":
        raise HTTPException(400, f"模型 {model} 需要账号池，请选择账号池模型生成")
    cfg = _require_relay_config()

    payload: dict = {"model": model, "prompt": req.prompt, "size": req.size, "n": req.n}
    if req.quality:
        payload["quality"] = req.quality

    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.post(f"{cfg['base_url']}/generations", headers=headers, json=payload)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream connection error: {e}")

    if r.status_code >= 400:
        return JSONResponse(
            status_code=_upstream_error_status(r.status_code),
            content={"error": _safe_json(r), "upstream_status": r.status_code},
        )
    return r.json()


# ---------- chat2api mode (new, for free ChatGPT account via reverse proxy) ----------

async def generate_via_chat2api(req: GenerateRequest, model: str):
    if _source_for_model(model) != "chat2api":
        raise HTTPException(400, f"模型 {model} 需要中转站，请选择 gpt-image-2")
    if not CHAT_KEY:
        raise HTTPException(500, "账号池生图 key 未配置，请联系管理员")

    headers = {"Authorization": f"Bearer {CHAT_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "prompt": req.prompt, "size": req.size, "n": req.n, "response_format": "b64_json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            r = await client.post(f"{CHAT_BASE}/images/generations", headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Upstream image error: {e}")

    if r.status_code >= 400:
        return JSONResponse(
            status_code=_upstream_error_status(r.status_code),
            content={"error": _safe_json(r), "upstream_status": r.status_code},
        )
    data = _safe_json(r)
    if isinstance(data, dict) and not data.get("model"):
        data["model"] = model
    return data


# ---------- routes ----------

@app.post("/api/generate")
async def generate(req: GenerateRequest, ident: dict = Depends(require_user)):
    _enforce_user_rate_limit(ident, req.n)
    model = _normalize_model(req.model)
    mode = _source_for_model(model)
    started = time.perf_counter()
    status_code = 200
    result = None
    try:
        async with _image_semaphore:
            if mode == "chat2api":
                result = await generate_via_chat2api(req, model)
            else:
                result = await generate_via_relay(req, model)
        status_code = result.status_code if isinstance(result, JSONResponse) else 200
        return result
    except HTTPException as e:
        status_code = e.status_code
        result = {"error": e.detail}
        raise
    finally:
        if result is not None:
            _record_usage(ident, "generate", mode, req.n, result, status_code, int((time.perf_counter() - started) * 1000))


async def _read_edit_image(image: UploadFile) -> bytes:
    content = await image.read(MAX_EDIT_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(400, "参考图不能为空")
    if len(content) > MAX_EDIT_IMAGE_BYTES:
        max_mb = MAX_EDIT_IMAGE_BYTES / (1024 * 1024)
        limit = f"{math.ceil(MAX_EDIT_IMAGE_BYTES / 1024)}KB" if max_mb < 1 else f"{max_mb:g}MB"
        raise HTTPException(413, f"参考图不能超过 {limit}")
    content_type = (image.content_type or "").lower()
    if content_type and content_type != "application/octet-stream" and content_type not in EDIT_IMAGE_TYPES:
        raise HTTPException(400, "参考图仅支持 PNG、JPG、WEBP")
    return content


@app.post("/api/edits")
async def edits(
    prompt: Annotated[str, Form()],
    image: Annotated[UploadFile, File()],
    size: Annotated[str, Form()] = "1024x1024",
    n: Annotated[int, Form()] = 1,
    quality: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    ident: dict = Depends(require_user),
):
    selected_model = _normalize_model(model)
    if _source_for_model(selected_model) != "relay":
        raise HTTPException(400, "参考图仅支持 gpt-image-2 / 中转站")

    if n < 1 or n > 4:
        raise HTTPException(400, "n must be between 1 and 4")

    _enforce_user_rate_limit(ident, n)
    mode = _source_for_model(selected_model)
    started = time.perf_counter()
    status_code = 200
    result = None
    cfg = _require_relay_config()
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    content = await _read_edit_image(image)
    files = {"image": (image.filename or "ref.png", content, image.content_type or "image/png")}
    data = {"model": selected_model, "prompt": prompt, "size": size, "n": str(n)}
    if quality:
        data["quality"] = quality
    try:
        async with _image_semaphore:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                try:
                    r = await client.post(f"{cfg['base_url']}/edits", headers=headers, data=data, files=files)
                except httpx.HTTPError as e:
                    raise HTTPException(502, f"Upstream connection error: {e}")
        if r.status_code >= 400:
            result = JSONResponse(
                status_code=_upstream_error_status(r.status_code),
                content={"error": _safe_json(r), "upstream_status": r.status_code},
            )
            status_code = result.status_code
            return result
        result = r.json()
        return result
    except HTTPException as e:
        status_code = e.status_code
        result = {"error": e.detail}
        raise
    finally:
        if result is not None:
            _record_usage(ident, "edits", mode, n, result, status_code, int((time.perf_counter() - started) * 1000))


@app.get("/api/health")
async def health(_: dict = Depends(require_user)):
    relay = _public_relay_config()
    mode = _current_mode()
    models = _model_options()
    default_model = _default_model()
    return {
        "ok": True,
        "mode": mode,
        "routing": "model",
        "model": default_model,
        "default_model": default_model,
        "models": models,
        "relay_base": relay["base_url"],
        "chat_base": CHAT_BASE,
        "key_loaded": any(item["ready"] for item in models),
        "edits_supported": any(item["supports_edits"] and item["ready"] for item in models),
        "c2a_admin": bool(C2A_BASE and C2A_KEY),
    }


@app.get("/livez")
async def livez():
    return {"ok": True}


@app.get("/api/settings/relay")
async def get_relay_settings(_: dict = Depends(require_admin)):
    return _public_relay_config()


@app.put("/api/settings/relay")
async def update_relay_settings(body: RelaySettingsBody, _: dict = Depends(require_admin)):
    return _save_relay_config(body)


@app.get("/api/settings/mode")
async def get_mode_settings(_: dict = Depends(require_admin)):
    return _public_mode_config()


@app.get("/api/usage")
async def get_usage(_: dict = Depends(require_admin)):
    return _usage_snapshot()


@app.put("/api/settings/mode")
async def update_mode_settings(body: ModeSettingsBody, _: dict = Depends(require_admin)):
    return _save_mode_config(body)


# ---------- chatgpt2api account-management proxy ----------

class TokenListBody(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    token_ids: list[str] = Field(default_factory=list)


def _ensure_c2a():
    if not (C2A_BASE and C2A_KEY):
        raise HTTPException(500, "C2A_BASE / C2A_KEY not configured (.env)")


async def _c2a_raw_request(method: str, path: str, *, json_body: dict | None = None) -> tuple[int, dict]:
    _ensure_c2a()
    url = f"{C2A_BASE}{path}"
    headers = {"Authorization": f"Bearer {C2A_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
        try:
            r = await client.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream c2a error: {e}")
    return r.status_code, _safe_json(r)


async def _c2a_request(method: str, path: str, *, json_body: dict | None = None) -> JSONResponse:
    status, content = await _c2a_raw_request(method, path, json_body=json_body)
    return JSONResponse(status_code=status, content=content)


def _mask_token(token: str) -> str:
    if len(token) <= 16:
        return "***"
    return f"{token[:8]}…{token[-6:]}"


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _public_account_item(item: dict) -> dict:
    public = _redact_c2a_response(item)
    token = str(item.get("access_token") or item.get("accessToken") or item.get("token") or "").strip()
    if token:
        public["token_id"] = _token_id(token)
        public["token_masked"] = _mask_token(token)
        public["access_token"] = _mask_token(token)
    return public


async def _resolve_account_tokens(tokens: list[str], token_ids: list[str]) -> list[str]:
    resolved = [t.strip() for t in tokens if t and t.strip()]
    wanted = {t.strip() for t in token_ids if t and t.strip()}
    if wanted:
        status, content = await _c2a_raw_request("GET", "/api/accounts")
        if status >= 400:
            raise HTTPException(status, _redact_c2a_response(content))
        by_id = {}
        for item in content.get("items") or []:
            if not isinstance(item, dict):
                continue
            token = str(item.get("access_token") or item.get("accessToken") or item.get("token") or "").strip()
            if token:
                by_id[_token_id(token)] = token
        missing = sorted(wanted - set(by_id))
        if missing:
            raise HTTPException(404, {"message": "account token id not found", "token_ids": missing})
        resolved.extend(by_id[token_id] for token_id in sorted(wanted))
    seen = set()
    unique = []
    for token in resolved:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _redact_c2a_response(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in {"access_token", "accesstoken", "refresh_token", "refreshtoken", "token", "tokens", "access_tokens", "accesstokens", "api_key", "apikey"}:
                if isinstance(item, list):
                    redacted[key] = [_mask_token(str(t)) for t in item]
                elif isinstance(item, str):
                    redacted[key] = _mask_token(item)
                else:
                    redacted[key] = "***"
            else:
                redacted[key] = _redact_c2a_response(item)
        return redacted
    if isinstance(value, list):
        return [_redact_c2a_response(item) for item in value]
    return value


def _response_deleted_count(data: dict) -> int:
    for key in ("deleted", "removed", "success", "count"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    items = data.get("items") or data.get("deleted_items") or []
    return len(items) if isinstance(items, list) else 0


def _delete_failed(status: int, data: dict) -> bool:
    if status in {400, 404, 405, 422}:
        return True
    if status >= 500:
        return True
    if status >= 400:
        return True
    if data.get("ok") is True:
        return False
    if data.get("ok") is False:
        return True
    return _response_deleted_count(data) == 0


async def _delete_c2a_accounts(tokens: list[str]) -> dict:
    attempts = []
    for method, path in (("DELETE", "/api/accounts"), ("POST", "/api/accounts/remove"), ("POST", "/api/accounts/delete")):
        status, data = await _c2a_raw_request(method, path, json_body={"tokens": tokens})
        attempts.append({"method": method, "path": path, "status": status, "response": data})
        if not _delete_failed(status, data):
            deleted = _response_deleted_count(data) or len(tokens)
            return {
                "ok": True,
                "deleted": deleted,
                "failed": max(len(tokens) - deleted, 0),
                "tokens": [_mask_token(t) for t in tokens],
                "upstream": {"method": method, "path": path, "status": status, "response": _redact_c2a_response(data)},
            }
        if status not in {400, 404, 405, 422} and status < 500:
            break
    last = attempts[-1]
    return {
        "ok": False,
        "deleted": 0,
        "failed": len(tokens),
        "tokens": [_mask_token(t) for t in tokens],
        "upstream": {**last, "response": _redact_c2a_response(last.get("response"))},
    }


def _image_remaining(item: dict) -> int | None:
    for progress in item.get("limits_progress") or []:
        if progress.get("feature_name") == "image_gen":
            remaining = progress.get("remaining")
            return remaining if isinstance(remaining, int) else None
    quota = item.get("quota")
    return quota if isinstance(quota, int) else None


def _is_normal_account(item: dict) -> bool:
    status = str(item.get("status") or "").strip().lower()
    return status in {"normal", "正常"}


def _cleanup_candidate(item: dict, body: CleanupAccountsBody) -> bool:
    token = str(item.get("access_token") or "").strip()
    if not token:
        return False
    status = str(item.get("status") or "").strip()
    if body.statuses and status in body.statuses:
        return True
    if not body.statuses and not _is_normal_account(item):
        return True
    return bool(body.zero_quota and _image_remaining(item) == 0)


def _account_summary(item: dict) -> dict:
    token = str(item.get("access_token") or "")
    return {
        "email": item.get("email") or "-",
        "status": item.get("status") or "未知",
        "remaining": _image_remaining(item),
        "token": _mask_token(token) if token else "",
    }


@app.get("/api/accounts")
async def list_accounts(_: dict = Depends(require_admin)):
    status, content = await _c2a_raw_request("GET", "/api/accounts")
    if isinstance(content, dict) and isinstance(content.get("items"), list):
        public = {**_redact_c2a_response(content), "items": []}
        public["items"] = [_public_account_item(item) if isinstance(item, dict) else item for item in content.get("items") or []]
    else:
        public = _redact_c2a_response(content)
    return JSONResponse(status_code=status, content=public)


@app.post("/api/accounts")
async def add_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = [t.strip() for t in body.tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "tokens is required")
    status, content = await _c2a_raw_request("POST", "/api/accounts", json_body={"tokens": tokens})
    return JSONResponse(status_code=status, content=_redact_c2a_response(content))


@app.post("/api/accounts/remove")
async def remove_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = await _resolve_account_tokens(body.tokens, body.token_ids)
    if not tokens:
        raise HTTPException(400, "tokens or token_ids is required")
    result = await _delete_c2a_accounts(tokens)
    return JSONResponse(status_code=200 if result["ok"] else 502, content=result)


@app.post("/api/accounts/cleanup")
async def cleanup_accounts(body: CleanupAccountsBody, _: dict = Depends(require_admin)):
    status, data = await _c2a_raw_request("GET", "/api/accounts")
    if status >= 400:
        return JSONResponse(status_code=status, content=data)
    items = data.get("items") or []
    candidates = [item for item in items if isinstance(item, dict) and _cleanup_candidate(item, body)]
    tokens = [str(item.get("access_token") or "").strip() for item in candidates]
    summaries = [_account_summary(item) for item in candidates]
    if body.dry_run:
        return {"ok": True, "dry_run": True, "count": len(tokens), "items": summaries}
    if not tokens:
        return {"ok": True, "dry_run": False, "count": 0, "deleted": 0, "items": []}
    result = await _delete_c2a_accounts(tokens)
    result.update({"dry_run": False, "count": len(tokens), "items": summaries})
    return JSONResponse(status_code=200 if result["ok"] else 502, content=result)


@app.post("/api/accounts/refresh")
async def refresh_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = await _resolve_account_tokens(body.tokens, body.token_ids)
    status, content = await _c2a_raw_request("POST", "/api/accounts/refresh", json_body={"access_tokens": tokens})
    return JSONResponse(status_code=status, content=_redact_c2a_response(content))


# ---------- user management (admin-only) ----------

class UserCreateBody(BaseModel):
    name: str = Field(default="", max_length=80)
    key: str = Field(default="", max_length=200)


class UserPatchBody(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None


def _public_user(u: dict) -> dict:
    return {
        "id": u.get("id", ""),
        "name": u.get("name", ""),
        "key": u.get("key", ""),
        "enabled": u.get("enabled", True),
        "created": u.get("created", ""),
        "last_used": u.get("last_used", None),
    }


@app.post("/api/auth/check")
async def auth_check(ident: dict = Depends(require_user)):
    return ident


@app.get("/api/users")
async def list_users(_: dict = Depends(require_admin)):
    data = _load_auth()
    return {"items": [_public_user(u) for u in data.get("users", [])]}


@app.post("/api/users")
async def create_user(body: UserCreateBody, _: dict = Depends(require_admin)):
    data = _load_auth()
    key = body.key.strip() or _gen_key("sk-app")
    if key == data.get("admin_token") or any(u.get("key") == key for u in data.get("users", [])):
        raise HTTPException(400, "key already exists")
    user = {
        "id": secrets.token_hex(8),
        "name": (body.name or "未命名").strip()[:80],
        "key": key,
        "enabled": True,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_used": None,
    }
    data.setdefault("users", []).append(user)
    _save_auth(data)
    return _public_user(user)


@app.patch("/api/users/{user_id}")
async def patch_user(user_id: str, body: UserPatchBody, _: dict = Depends(require_admin)):
    data = _load_auth()
    for u in data.get("users", []):
        if u.get("id") == user_id:
            if body.name is not None:
                u["name"] = body.name.strip()[:80]
            if body.enabled is not None:
                u["enabled"] = bool(body.enabled)
            _save_auth(data)
            return _public_user(u)
    raise HTTPException(404, "user not found")


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str, _: dict = Depends(require_admin)):
    data = _load_auth()
    before = len(data.get("users", []))
    data["users"] = [u for u in data.get("users", []) if u.get("id") != user_id]
    if len(data["users"]) == before:
        raise HTTPException(404, "user not found")
    _save_auth(data)
    return {"ok": True}


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/admin")
async def admin():
    return FileResponse("static/admin.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
