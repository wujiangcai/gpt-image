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

编辑 `.env`：

```
IMAGE_API_KEY=你的真实 key
IMAGE_API_BASE=https://otokapi.com/v1/images   # 改成你中转商的地址
IMAGE_MODEL=gpt-image-2                         # 改成你中转商支持的模型
```

启动：

```bash
python main.py
```

打开浏览器：http://127.0.0.1:8000

页面顶部会显示当前加载的 base 和 model，确认无误后开始用。

## 支持的请求

| 端点 | 用途 | 上游 |
|------|------|------|
| `POST /api/generate` | JSON 请求，纯文本 prompt 生图 | `{API_BASE}/generations` |
| `POST /api/edits` | multipart 请求，带参考图 | `{API_BASE}/edits` |
| `GET /api/health` | 健康检查 + 当前配置 | 本地 |

后端只做**转发**，不改请求体（除了把 model 字段从 `.env` 注入），上游返回什么就返回什么。

## 切换中转商 / 切换官方

只改 `.env` 三个变量，重启服务：

```
# 切到 OpenAI 官方
IMAGE_API_KEY=sk-proj-xxx
IMAGE_API_BASE=https://api.openai.com/v1/images
IMAGE_MODEL=gpt-image-1

# 切到中转
IMAGE_API_KEY=中转商给的key
IMAGE_API_BASE=https://your-relay.com/v1/images
IMAGE_MODEL=gpt-image-2
```

## 常见错误排查

页面顶部红条显示 `IMAGE_API_KEY 未配置` → `.env` 没填或没重启。

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
│   └── index.html       # 前端（暗色主题，带参考图、历史记录）
├── requirements.txt
├── .env.example
└── .env                 # 你自己创建，放 key
```
