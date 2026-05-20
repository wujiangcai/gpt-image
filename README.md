# Image Gen Demo (Backend Proxy)

支持 OpenAI 官方 / 任意 OpenAI 兼容中转商的图片生成 demo。

**架构**：浏览器 → 本地 FastAPI（端口 8000）→ 你配置的 API Base URL（OpenAI / otokapi / modaplex / 其他中转）

**为什么需要后端代理而不是浏览器直连**：浏览器对 `file://` 和跨域 API 有 CORS 限制，直连会被拦；走本地后端就是同源请求，没限制；而且 API key 也不会暴露在浏览器里。

## 启动步骤

```bash
cd image-gen-demo

# 已经创建过 venv 的话跳过这步
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# 复制 .env.example 到 .env
copy .env.example .env    # Windows
# cp .env.example .env    # macOS/Linux
```

编辑 `.env` 设置默认值（也可启动后在 `/admin` 页面覆盖）：

```
MODE=relay
IMAGE_API_KEY=你的真实 key
IMAGE_API_BASE=https://otokapi.com/v1/images   # 改成你中转商的图片 API 路径
IMAGE_MODEL=gpt-image-2                         # 改成你中转商支持的模型
```

如果 `.env` 未设置 `ADMIN_TOKEN`，服务首次启动会在日志里生成一个管理员 token，并保存到 `_auth.json`。

启动：

```bash
python main.py
```

打开浏览器：http://127.0.0.1:8000

页面顶部会显示当前加载的 base 和 model，确认无误后开始用。

## 管理员配置生图来源

打开 `/admin`，用管理员 token 登录后，先在「生图来源」里选择运行模式：

- `中转站 Relay`：通过 OpenAI 兼容图片接口生图，支持纯文本生图和参考图编辑。
- `账号池 ChatGPT`：通过配置的 ChatGPT 账号池生图，只支持纯文本 prompt；参考图编辑会提示切回中转站模式。

选择保存后立即生效，不需要重启服务。当前模式会保存到 `_auth.json` 的 `settings.mode` 字段；未保存时回退使用 `.env` 的 `MODE`。

## 管理员配置中转站

打开 `/admin`，用管理员 token 登录后，在「中转站生图设置」里填写：

- `API 路径`：OpenAI 兼容图片接口路径，通常是 `https://your-relay.com/v1/images`
- `模型`：例如 `gpt-image-2`
- `API Key`：中转站提供的 key

保存后立即生效，不需要重启服务。`API Key` 不会回显给前端，普通用户只能通过 `/api/health` 看到是否已配置。管理员配置会保存到 `_auth.json` 的 `relay` 字段；如果未保存管理员配置，则回退使用 `.env` 里的 `IMAGE_API_BASE`、`IMAGE_API_KEY`、`IMAGE_MODEL`。

## 账号池管理

管理员可在 `/admin` 添加 ChatGPT `access_token` 到账号池、同步额度、删除单个账号。删除账号时后端会自动尝试多种上游兼容接口，减少因上游不支持 `DELETE` JSON body 导致的删除失败。

「当前账号池」支持批量清理异常账号：

- `预览异常账号`：只显示将被清理的账号摘要，不会删除。
- `清理异常账号`：删除状态不是 `normal`/`正常` 的账号。
- `包含零额度`：勾选后也会清理图片剩余额度为 0 的账号。

预览和清理结果只显示脱敏 token，不会把完整 `access_token` 回显到页面。

## 用户密钥

管理员可在 `/admin` 的「用户密钥管理」里创建 `sk-app-*` 访问密钥。用户用该密钥登录首页后可以画图，但不能访问管理员页面或修改中转站配置。

## 支持的请求

| 端点 | 用途 | 上游 |
|------|------|------|
| `POST /api/generate` | JSON 请求，纯文本 prompt 生图 | `{API_BASE}/generations` |
| `POST /api/edits` | multipart 请求，带参考图 | `{API_BASE}/edits` |
| `GET /api/health` | 健康检查 + 当前公开配置 | 本地 |
| `GET /api/settings/relay` | 管理员查看中转站配置状态 | 本地 |
| `PUT /api/settings/relay` | 管理员保存中转站 API 路径 / 模型 / key | 本地 |
| `GET /api/settings/mode` | 管理员查看当前生图来源 | 本地 |
| `PUT /api/settings/mode` | 管理员切换 `relay` / `chat2api` | 本地 |
| `GET /api/accounts` | 管理员查看账号池 | chatgpt2api |
| `POST /api/accounts/remove` | 管理员删除账号，带兼容 fallback | chatgpt2api |
| `POST /api/accounts/cleanup` | 管理员预览或批量清理异常账号 | chatgpt2api |

后端只做**转发**，不改请求体（除了把 model 字段从管理员配置或 `.env` 注入），上游返回什么就返回什么。

## 切换中转商 / 切换官方

推荐在 `/admin` 的「中转站生图设置」里直接切换，保存后立即生效：

```
API 路径: https://api.openai.com/v1/images
模型: gpt-image-1
API Key: sk-proj-xxx

API 路径: https://your-relay.com/v1/images
模型: gpt-image-2
API Key: 中转商给的 key
```

也可以修改 `.env` 作为默认值，然后重启服务；但一旦 `/admin` 保存过配置，运行时会优先使用 `_auth.json` 里的管理员配置。

## 本地验证

```bash
python -m py_compile main.py tests/test_admin_features.py
.venv\Scripts\python.exe -m unittest tests.test_admin_features
```

`tests/test_admin_features.py` 覆盖：运行时生图来源保存与 `/api/health` 同步、账号删除 fallback、异常账号预览/清理与 token 脱敏。

## 常见错误排查

页面顶部红条显示 `中转站 API key 未配置` → 用管理员 token 登录 `/admin`，在「中转站生图设置」里填写 API Key；或检查 `.env` 的 `IMAGE_API_KEY`。

生成时 `[401] Unauthorized` → key 错了，或者中转商认证方式不是 `Bearer`（需要改 main.py 的 headers）。

`[404] Not Found` → base URL 路径错了，常见错误：少了 `/v1/images`，或多带了末尾斜杠（代码会去掉斜杠，但路径段错了无法挽救）。

`[400] model not supported` → `IMAGE_MODEL` 名字写错了，问中转商支持哪些。

`[502] Upstream connection error` → 网络通不到中转商，检查能不能 ping 通、是否需要代理。

`[400] content_policy_violation` → prompt 触发审核，换个表述。

## 文件结构

```
image-gen-demo/
├── main.py              # FastAPI 代理后端
├── static/
│   ├── index.html       # 前端（暗色主题，带参考图、历史记录）
│   ├── admin.html       # 管理员页面（中转站设置、账号池、用户密钥）
│   └── auth.js          # 前端共享登录逻辑
├── requirements.txt
├── .env.example
├── .env                 # 本地默认配置，不提交
└── _auth.json           # 管理员 token、用户密钥、中转站配置，不提交
```
