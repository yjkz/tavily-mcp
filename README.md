# Tavily Pool MCP Gateway

把多个 Tavily API key 组成一个统一调度的 key 池,以 MCP(Model Context Protocol)服务的形式安全地暴露到公网,并附带一个可视化管理控制台。

```
MCP 客户端 (Claude Code / Cursor / …)
    │  Authorization: Bearer tpm_xxx(你在控制台签发的访问 Token)
    ▼
┌──────────────────────────────────────────────┐
│  Nginx(你的域名,HTTPS)                       │
│  └─ 单进程网关(仅监听 127.0.0.1:8000)        │
│  ├─ /mcp       MCP Streamable HTTP 端点      │
│  ├─ /          Dashboard 控制台(密码登录)  │
│  ├─ /api/*     管理 API(会话鉴权)          │
│  └─ /health    健康检查                      │
│                                              │
│  Key 池调度器:轮询 → 429 冷却 → 432/433      │
│  标记耗尽 → 401 禁用 → 自动换 key 重试        │
│  每 6h 调 GET /usage 校准真实配额             │
└──────────────────────────────────────────────┘
    │ 轮流使用, 均摊消耗
    ▼
  Tavily API(key1 / key2 / … 几十个均可)
```

## 功能

- **Key 池调度**:轮询均摊消耗;上游 429 自动冷却并换 key 重试,432/433(配额耗尽)自动标记、月初重置并经 `/usage` 校准后恢复,401(无效)禁用并在控制台告警;参数错误(4xx)直接透传不浪费重试。
- **MCP 工具**:`tavily_search`、`tavily_extract`、`tavily_crawl`、`tavily_map`(crawl/map 需要完整等级 Token)、`get_my_usage`(客户端自查用量)。
- **鉴权**:你签发的 `tpm_` 前缀 Bearer Token(仅存 SHA-256 哈希,可随时吊销即时生效);每个 Token 独立配置 RPM 限流、日请求配额、月 Credits 上限。
- **Dashboard**(中文界面,默认深色):
  - 概览:今日请求 / 本月 Credits / Key 池健康 / 14 天趋势图 / 池配额总览
  - Key 池:卡片管理、**测试连接按钮**(零成本验证 key 有效性并校准真实剩余配额)、批量粘贴添加、禁用/启用/删除
  - 访问 Token:签发(明文仅显示一次)、吊销、限额调整、用量统计
  - 请求日志:按 Token/状态/工具筛选,分页,保留 30 天
- **安全**:登录失败 5 次锁定 10 分钟;会话签名 Cookie(HttpOnly);Tavily key 永不下发给客户端。

## 快速开始(公网部署)

前置:一台有公网 IP 的 VPS(1C1G 足够),域名 DNS A 记录指向它,已安装 Docker、Nginx 和 HTTPS 证书自动续期服务。

```bash
git clone <你的仓库地址> tavily-pool-mcp && cd tavily-pool-mcp

cp .env.example .env
# 编辑 .env:设置 ADMIN_PASSWORD(强密码)

docker compose up -d --build
```

Compose 会把服务发布为 `127.0.0.1:8000`，不直接对公网开放。将 Nginx 的对应站点反向代理到该地址；以下 `location` 配置应放入你现有的 TLS `server` 块中：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

`proxy_buffering off` 和较长的 `proxy_read_timeout` 是 MCP Streamable HTTP 长连接所需。确认 Nginx 配置后重载 Nginx，再打开 `https://你的域名/` 登录控制台:

1. **Key 池 → 批量添加**:粘贴你的 Tavily key(每行一个),点每个卡片的 **测试连接** 验证。
2. **访问 Token → 创建 Token**:选等级、限额,复制只显示一次的 `tpm_...`。
3. 在 MCP 客户端里配置使用。

## 客户端配置

假设你的地址是 `https://mcp.example.com/mcp`,Token 是 `tpm_xxx`。两种鉴权方式任选:

**方式一:URL 带 Token(推荐,和 Tavily 官方 MCP 同款形式,无需配置请求头)**

```
https://mcp.example.com/mcp?token=tpm_xxx
```

**Claude Code:**

```bash
claude mcp add --transport http tavily-pool "https://mcp.example.com/mcp?token=tpm_xxx"
```

**Cursor / 通用 MCP JSON 配置:**

```json
{
  "mcpServers": {
    "tavily-pool": { "url": "https://mcp.example.com/mcp?token=tpm_xxx" }
  }
}
```

**方式二:Authorization 请求头**

```bash
claude mcp add --transport http tavily-pool https://mcp.example.com/mcp \
  --header "Authorization: Bearer tpm_xxx"
```

```json
{
  "mcpServers": {
    "tavily-pool": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer tpm_xxx" }
    }
  }
}
```

任何支持 Streamable HTTP 传输的 MCP 客户端都可以用同样的方式接入。控制台创建 Token 成功时会直接给出可复制的 MCP 地址和 Claude Code 命令。

> 提示:URL 带 Token 的形式会把凭证留在 URL 里,注意不要提交到公开仓库或分享给不可信的人;介意的话用请求头方式,泄露时在控制台吊销即可。

## 本地开发

```bash
# 后端(Python 3.12+,uv)
uv sync
TAVILY_POOL_DEV=1 uv run uvicorn app.main:app --port 8000
#   开发模式默认密码 admin,Cookie 不加 Secure

# 前端(另开终端)
cd dashboard && npm install && npm run dev
#   Vite 开发服务器 http://localhost:5173,/api 与 /mcp 已代理到 8000

# 测试
uv run pytest
```

## 配置项

见 `.env.example`,全部有合理默认值:冷却时长、重试次数、配额校准频率、日志保留天数、默认 RPM、输出截断长度等。

## 架构说明

- 单进程 Starlette(由 FastMCP `http_app()` 提供)+ uvicorn,MCP、管理 API、静态前端共用 8000 端口,部署面最小。
- SQLite(WAL)三张表:`tavily_keys`(池状态)、`access_tokens`(哈希后的访问凭证)、`request_logs`(审计与统计)。小团队规模下无需聚合表,直接 SQL 聚合。
- Token 校验器与限流/配额检查分别实现在 `TokenVerifier` 子类与 MCP 中间件(`app/mcp_server.py`),新增工具自动纳入管控。
- 后台任务(`app/tasks.py`):每 6 小时校准全部 key 的真实配额(自动发现月初重置并恢复耗尽 key),每 6 小时清理过期日志。

## 安全须知

- Tavily key 以明文存于服务器 SQLite(需原样转发上游,加密收益有限)。请确保 `.env`、`data/` 目录权限,不要把 `data/` 提交进仓库。
- 面向公网务必:强 `ADMIN_PASSWORD`、HTTPS(由 Nginx 终止 TLS)、按需收紧 Token 限额。RPM/日配额/月 Credits 三层限额就是防滥用的止损开关。
- 访问 Token 泄露时,在控制台吊销即可,全部客户端立即失效。
