# VPS 部署指南（Ubuntu + Docker compose）

目标：把 image-gen-demo + chatgpt2api 部署到旧金山 VPS。建议只把服务绑定到本机端口，再通过 Caddy/Nginx/Cloudflare Tunnel 提供 HTTPS；不要长期把 bearer token 暴露在公网 HTTP 上。

---

## 0. 准备清单

- [ ] VPS 公网 IP（下面用 `<VPS_IP>` 占位）
- [ ] SSH 能登（root 或带 sudo 的用户）
- [ ] 本机这个 `image-gen-demo` 文件夹（已经包含我刚加的 `Dockerfile`、`docker-compose.yml`、`.env.deploy.example`、`c2a-config.json.example`）

---

## 1. 本机：把代码 scp 到 VPS

> 在你**本地 PowerShell / Git Bash** 跑（注意 Windows 下 scp 路径用正斜杠）：

```bash
# 假设 VPS 用户名是 ubuntu，IP 是 1.2.3.4
cd C:/Users/caiwujiang/Desktop/image
scp -r image-gen-demo ubuntu@<VPS_IP>:~/
```

会拷过去：`~/image-gen-demo/` 整个目录。

> ⚠️ scp 不读 .dockerignore，**会连本地 `.env`（含本机 admin token）和 `_auth.json` 一起拷过去**。下面第 3 步会让你在 VPS 上把这俩**删掉重建**，避免本机/VPS 凭据混用。

---

## 2. VPS：装 Docker（如果没装）

SSH 上去：
```bash
ssh ubuntu@<VPS_IP>
```

跑这一段（Ubuntu 22.04/24.04 通用）：
```bash
# 一键脚本
curl -fsSL https://get.docker.com | sudo sh

# 把当前用户加进 docker 组（免 sudo）
sudo usermod -aG docker $USER

# 让组生效（要么重新登录，要么这一句）
newgrp docker

# 验证
docker --version
docker compose version
```

---

## 3. VPS：清理本机残留，配新 .env

```bash
cd ~/image-gen-demo

# 先把从本机拷来的本机凭据删掉
rm -f .env _auth.json
rm -rf .venv __pycache__ c2a-data .claude

# 生成两段随机字符串
openssl rand -base64 32 | tr -d /+= | head -c 40 ; echo   # 抄下来当 ADMIN_TOKEN
openssl rand -base64 32 | tr -d /+= | head -c 40 ; echo   # 抄下来当 C2A_KEY

# 复制模板
cp .env.deploy.example .env
cp c2a-config.json.example c2a-config.json

# 编辑 .env，把刚生成的两段填进去
nano .env

# bind mount 的占位文件必须先存在（否则 Docker 会当目录建出来）
touch _auth.json
chmod 600 .env _auth.json c2a-config.json
```

`.env` 应该长这样：
```
ADMIN_TOKEN=ABCDEFGHIJKLMNOP1234567890qwertyuiopASDFG
C2A_KEY=ZXCVBNM987654321qwertyuiopASDFGHJKLzxcvbn
```

> ADMIN_TOKEN 你登录 admin 页面用，**自己保存好**。
> C2A_KEY 是 image-gen-demo 内部调 chatgpt2api 用的，用户根本看不到，随机就行。

---

## 4. VPS：拉镜像 + 启动

```bash
# 第一次会拉 chatgpt2api 镜像 + 构建 image-gen-demo 镜像，几分钟
docker compose up -d --build

# 看状态
docker compose ps
# 看日志
docker compose logs -f
# 按 Ctrl+C 退出 logs（不会停服务）
```

正常应该看到：
```
NAME             STATUS
chatgpt2api      Up
image-gen-demo   Up
```

---

## 5. VPS：配置 HTTPS 入口

```bash
# 只开放 HTTPS 入口，不直接开放 image-gen-demo 的 8080 端口
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp
sudo ufw status

# 如果 VPS 提供商（搬瓦工/Linode/DO/AWS）有控制台防火墙，
# 也只放行 80/443，或使用 Cloudflare Tunnel 不开放入站端口。
```

用 Caddy/Nginx/Cloudflare Tunnel 把公网 HTTPS 反代到 `127.0.0.1:8080`。`docker-compose.yml` 默认只监听本机地址，避免把 bearer token 暴露在公网 HTTP。

---

## 6. 测试访问

如果还没有 HTTPS 反代，只在 VPS 本机先测：
```
curl http://localhost:8080/livez
```

配置好 HTTPS 入口后，本机浏览器开：
```
https://<你的域名>
```

应该看到登录框 → 粘贴你刚才生成的 `ADMIN_TOKEN` → 登录成功 → 进画图主页。

去 `https://<你的域名>/admin`：
1. 在「生图来源」选择 `中转站 Relay` 或 `账号池 ChatGPT`，保存后无需重启。
2. 在「中转站生图设置」里填写中转站图片 API 路径（例如 `https://your-relay.com/v1/images`）、模型（例如 `gpt-image-2`）和 API Key。
3. 如使用内置 chatgpt2api 号池，继续添加 ChatGPT access_token → 看到额度。
4. 在「当前账号池」可先点「预览异常账号」，确认后点「清理异常账号」批量移除异常账号；需要清理零额度账号时勾选「包含零额度」。
5. 在「用户密钥管理」创建用户密钥 → 把密钥发给同事。

---

## 7. 常用运维命令

```bash
cd ~/image-gen-demo

# 重启
docker compose restart

# 看实时日志
docker compose logs -f image-gen-demo
docker compose logs -f chatgpt2api

# 停服务
docker compose down

# 拉最新 chatgpt2api 镜像 + 重启
docker compose pull chatgpt2api
docker compose up -d

# 改了 main.py 或 static/ 后重新构建 image-gen-demo
docker compose up -d --build image-gen-demo
```

---

## 8. 安全建议

1. **不要公网 HTTP**：管理员 token、用户密钥和账号池 token 都是 bearer 凭据，必须走 HTTPS 或可信隧道。
2. **80/443 已被占**：可以选这些路：
   - 在原网站的 nginx/caddy 上加一个 `location /image/` 反代到 `127.0.0.1:8080`
   - 用 [Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/) 免费给一个 *.trycloudflare.com 域名 + 自动 HTTPS
   - 弄个域名，跑 Caddy 在另一个端口（比如 8443）
3. **管 token 像管密码**：admin token 泄了同事的画图额度也没了。
4. **定期更新**：`docker compose pull chatgpt2api` 拉作者 bugfix。

---

## 9. 常见问题

**Q：浏览器打不开**
- 防火墙 `sudo ufw status`
- VPS 提供商控制台防火墙或 Tunnel 状态
- `docker compose ps` 看容器在不在
- `curl http://localhost:8080/livez` VPS 自己能不能通

**Q：admin 登录后画图 502 / 出错**
- 先检查 `/admin` →「生图来源」当前选的是 `中转站 Relay` 还是 `账号池 ChatGPT`
- 走中转站时，检查「中转站生图设置」里的 API 路径是否包含 `/v1/images`，模型名是否为中转商支持的值
- 中转商 key 错误通常会返回 401/403
- chatgpt2api 容器日志：`docker compose logs chatgpt2api`
- 如果走内置号池，可能没加 access_token，去 admin 加一个
- access_token 可能失效（10 天有效期），admin 删旧加新；删不掉或异常账号多时，用「预览异常账号」和「清理异常账号」批量处理

**Q：chatgpt2api 连不上 chatgpt.com**
- 旧金山 VPS 一般直连没问题
- `docker compose exec chatgpt2api wget -qO- https://chatgpt.com` 看响应
- 如果不通，往 `c2a-config.json` 的 `"proxy"` 字段填代理地址

**Q：怎么把同事接进来**
1. 你用 ADMIN_TOKEN 登 `https://<你的域名>/admin`
2. 在「用户密钥管理」填备注名 → 创建
3. 复制 `sk-app-xxx` 发给同事
4. 同事浏览器开 `https://<你的域名>`，粘贴密钥登录就能画图
