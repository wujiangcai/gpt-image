import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

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

# Relay mode (existing): forward OpenAI Images API to a real OpenAI-compatible endpoint
RELAY_BASE = os.getenv("IMAGE_API_BASE", "https://api.openai.com/v1/images").rstrip("/")
RELAY_KEY = os.getenv("IMAGE_API_KEY", "")

# chat2api mode: translate /v1/images/generations -> /v1/chat/completions, parse markdown for image URLs
CHAT_BASE = os.getenv("CHAT_API_BASE", "http://127.0.0.1:3000/v1").rstrip("/")
CHAT_KEY = os.getenv("CHAT_API_KEY", "")
PROXY = os.getenv("HTTP_PROXY") or os.getenv("PROXY") or None

# chatgpt2api account-management proxy (browser hits image-gen-demo, server forwards with hidden key)
C2A_BASE = os.getenv("C2A_BASE", "").rstrip("/")
C2A_KEY = os.getenv("C2A_KEY", "")

# ---------- auth (admin token + user keys) ----------

_AUTH_FILE = Path(__file__).parent / "_auth.json"


def _gen_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def _load_auth() -> dict:
    if _AUTH_FILE.exists():
        try:
            return json.loads(_AUTH_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"admin_token": "", "users": []}


def _save_auth(data: dict) -> None:
    _AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


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
print(f"[auth] admin token = {_auth_state['admin_token']}  (在 .env 设 ADMIN_TOKEN 可固定)")
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

print(f"[ok] MODE={MODE}  MODEL={MODEL}")
if MODE == "chat2api":
    print(f"     chat completions endpoint = {CHAT_BASE}/chat/completions")
    print(f"     proxy for image download  = {PROXY or 'none'}")
else:
    print(f"     relay base = {RELAY_BASE}")

app = FastAPI(title="Image Gen Adapter")

_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"
    n: int = Field(default=1, ge=1, le=4)
    quality: Optional[str] = None


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:1000]}


def extract_image_urls(markdown: str) -> list:
    return _IMG_RE.findall(markdown or "")


# ---------- relay mode (legacy, for otokapi / OpenAI official) ----------

async def generate_via_relay(req: GenerateRequest):
    if not RELAY_KEY:
        raise HTTPException(500, "IMAGE_API_KEY not configured (.env)")

    payload: dict = {"model": MODEL, "prompt": req.prompt, "size": req.size, "n": req.n}
    if req.quality:
        payload["quality"] = req.quality

    headers = {"Authorization": f"Bearer {RELAY_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.post(f"{RELAY_BASE}/generations", headers=headers, json=payload)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream connection error: {e}")

    if r.status_code >= 400:
        return JSONResponse(
            status_code=r.status_code,
            content={"error": _safe_json(r), "upstream_status": r.status_code},
        )
    return r.json()


# ---------- chat2api mode (new, for free ChatGPT account via reverse proxy) ----------

async def generate_via_chat2api(req: GenerateRequest):
    if not CHAT_KEY:
        raise HTTPException(500, "CHAT_API_KEY not configured (.env)")

    # Augment prompt with size hint since chat-driven image gen doesn't take size param directly
    aug = req.prompt
    if req.size and req.size != "auto":
        aug = f"{req.prompt}\n\n(Image size: {req.size})"

    chat_payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": aug}],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {CHAT_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.post(f"{CHAT_BASE}/chat/completions", headers=headers, json=chat_payload)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream chat error: {e}")

    if r.status_code >= 400:
        return JSONResponse(
            status_code=r.status_code,
            content={"error": _safe_json(r), "upstream_status": r.status_code, "stage": "chat_completion"},
        )

    chat_resp = r.json()
    try:
        content = chat_resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return JSONResponse(status_code=502, content={"error": "unexpected chat response shape", "raw": chat_resp})

    image_urls = extract_image_urls(content)
    if not image_urls:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": "Model returned no image. Could mean image-gen not triggered, account lacks capability, or rate-limited.",
                    "model_response": content[:800],
                    "model": chat_resp.get("model"),
                    "usage": chat_resp.get("usage"),
                }
            },
        )

    # Download each image URL with system proxy (URLs are on OpenAI CDN, may need proxy from CN)
    images = []
    client_args: dict = {"timeout": TIMEOUT}
    if PROXY:
        client_args["proxy"] = PROXY
    async with httpx.AsyncClient(**client_args) as client:
        for url in image_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    b64 = base64.b64encode(resp.content).decode()
                    images.append({"b64_json": b64, "revised_prompt": req.prompt})
                else:
                    images.append({"url": url, "error": f"download status {resp.status_code}"})
            except Exception as e:
                images.append({"url": url, "error": f"download exception: {e}"})

    return {"created": int(time.time()), "data": images, "model": MODEL}


# ---------- routes ----------

@app.post("/api/generate")
async def generate(req: GenerateRequest, _: dict = Depends(require_user)):
    if MODE == "chat2api":
        return await generate_via_chat2api(req)
    return await generate_via_relay(req)


@app.post("/api/edits")
async def edits(
    prompt: Annotated[str, Form()],
    image: Annotated[UploadFile, File()],
    size: Annotated[str, Form()] = "1024x1024",
    n: Annotated[int, Form()] = 1,
    _: dict = Depends(require_user),
):
    if MODE == "chat2api":
        # chat2api supports multimodal upload via different mechanism — not implemented yet
        raise HTTPException(501, "Image edits via chat2api mode not implemented yet — use relay mode for /edits")

    if not RELAY_KEY:
        raise HTTPException(500, "IMAGE_API_KEY not configured (.env)")
    headers = {"Authorization": f"Bearer {RELAY_KEY}"}
    files = {"image[]": (image.filename or "ref.png", await image.read(), image.content_type or "image/png")}
    data = {"model": MODEL, "prompt": prompt, "size": size, "n": str(n)}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.post(f"{RELAY_BASE}/edits", headers=headers, data=data, files=files)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream connection error: {e}")
    if r.status_code >= 400:
        return JSONResponse(status_code=r.status_code, content={"error": _safe_json(r), "upstream_status": r.status_code})
    return r.json()


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "mode": MODE,
        "model": MODEL,
        "relay_base": RELAY_BASE if MODE == "relay" else None,
        "chat_base": CHAT_BASE if MODE == "chat2api" else None,
        "proxy": PROXY,
        "key_loaded": bool(RELAY_KEY if MODE == "relay" else CHAT_KEY),
        "c2a_admin": bool(C2A_BASE and C2A_KEY),
    }


# ---------- chatgpt2api account-management proxy ----------

class TokenListBody(BaseModel):
    tokens: list[str] = Field(default_factory=list)


def _ensure_c2a():
    if not (C2A_BASE and C2A_KEY):
        raise HTTPException(500, "C2A_BASE / C2A_KEY not configured (.env)")


async def _c2a_request(method: str, path: str, *, json_body: dict | None = None) -> JSONResponse:
    _ensure_c2a()
    url = f"{C2A_BASE}{path}"
    headers = {"Authorization": f"Bearer {C2A_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            r = await client.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream c2a error: {e}")
    return JSONResponse(status_code=r.status_code, content=_safe_json(r))


@app.get("/api/accounts")
async def list_accounts(_: dict = Depends(require_admin)):
    return await _c2a_request("GET", "/api/accounts")


@app.post("/api/accounts")
async def add_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = [t.strip() for t in body.tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "tokens is required")
    return await _c2a_request("POST", "/api/accounts", json_body={"tokens": tokens})


@app.post("/api/accounts/remove")
async def remove_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = [t.strip() for t in body.tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "tokens is required")
    return await _c2a_request("DELETE", "/api/accounts", json_body={"tokens": tokens})


@app.post("/api/accounts/refresh")
async def refresh_accounts(body: TokenListBody, _: dict = Depends(require_admin)):
    tokens = [t.strip() for t in body.tokens if t and t.strip()]
    return await _c2a_request("POST", "/api/accounts/refresh", json_body={"access_tokens": tokens})


# ---------- user management (admin-only) ----------

class UserCreateBody(BaseModel):
    name: str = Field(default="", max_length=80)


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
    user = {
        "id": secrets.token_hex(8),
        "name": (body.name or "未命名").strip()[:80],
        "key": _gen_key("sk-app"),
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
