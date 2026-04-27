// Shared auth helpers for index.html / admin.html
const AUTH_LS_KEY = 'app_token';

function getToken() { return localStorage.getItem(AUTH_LS_KEY) || ''; }
function setToken(t) { localStorage.setItem(AUTH_LS_KEY, t); }
function clearToken() { localStorage.removeItem(AUTH_LS_KEY); }

async function authedFetch(url, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const t = getToken();
  if (t) headers.set('Authorization', 'Bearer ' + t);
  return fetch(url, { ...opts, headers });
}

async function checkAuth() {
  if (!getToken()) return null;
  try {
    const r = await authedFetch('/api/auth/check', { method: 'POST' });
    if (!r.ok) {
      if (r.status === 401) clearToken();
      return null;
    }
    return await r.json();
  } catch (e) { return null; }
}

function logout() { clearToken(); location.reload(); }

function showLoginPanel(opts = {}) {
  const adminOnly = !!opts.adminOnly;
  if (document.getElementById('__loginPanel')) return;
  const panel = document.createElement('div');
  panel.id = '__loginPanel';
  panel.style.cssText = 'position:fixed;inset:0;background:rgba(10,10,15,0.94);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
  panel.innerHTML = `
    <div style="background:#14141f;border:1px solid #2a2a3e;border-radius:14px;padding:30px;max-width:440px;width:92%;box-shadow:0 16px 48px rgba(0,0,0,0.6)">
      <h2 style="font-size:1.3rem;background:linear-gradient(135deg,#7c5cfc,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px;font-weight:700">🔒 需要登录</h2>
      <p style="font-size:.82rem;color:#8888a0;margin-bottom:18px;line-height:1.6">${adminOnly ? '此页面需要<strong style="color:#fbbf24">管理员</strong>权限。请输入管理员 token。' : '请输入你的访问 token（管理员 token 或 sk-app- 用户密钥都可以）。'}</p>
      <input type="password" id="__loginInput" placeholder="paste your token…" style="width:100%;background:#1e1e2e;border:1px solid #2a2a3e;border-radius:10px;padding:12px 14px;color:#e2e2f0;font-size:.88rem;font-family:'SF Mono',Consolas,monospace;outline:none;margin-bottom:12px;box-sizing:border-box">
      <div id="__loginError" style="font-size:.78rem;color:#f87171;margin-bottom:14px;min-height:16px;text-align:center"></div>
      <button id="__loginBtn" style="width:100%;background:linear-gradient(135deg,#7c5cfc,#6d4de8);color:#fff;border:none;border-radius:10px;padding:11px;font-size:.92rem;font-weight:600;cursor:pointer;transition:transform .15s">登录</button>
      <p style="font-size:.7rem;color:#8888a0;margin-top:14px;text-align:center;line-height:1.5">第一次使用？admin token 在服务启动日志里（<code style="background:#1e1e2e;padding:1px 5px;border-radius:3px;color:#a78bfa">[auth] admin token = ...</code>）</p>
    </div>
  `;
  document.body.appendChild(panel);
  const input = panel.querySelector('#__loginInput');
  const btn = panel.querySelector('#__loginBtn');
  const err = panel.querySelector('#__loginError');
  const submit = async () => {
    const t = input.value.trim();
    if (!t) { err.style.color = '#f87171'; err.textContent = '请输入 token'; return; }
    err.style.color = '#8888a0'; err.textContent = '验证中…';
    setToken(t);
    const id = await checkAuth();
    if (!id) { err.style.color = '#f87171'; err.textContent = 'token 无效或已被禁用'; return; }
    if (adminOnly && id.role !== 'admin') {
      err.style.color = '#f87171'; err.textContent = '该 token 不是管理员，无法访问';
      clearToken(); return;
    }
    panel.remove();
    if (typeof opts.onSuccess === 'function') opts.onSuccess(id);
    else location.reload();
  };
  btn.addEventListener('click', submit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  setTimeout(() => input.focus(), 100);
}

async function ensureAuth(adminOnly = false) {
  const id = await checkAuth();
  if (!id || (adminOnly && id.role !== 'admin')) {
    return new Promise(resolve => {
      showLoginPanel({ adminOnly, onSuccess: resolve });
    });
  }
  return id;
}
