# 05 · 管理 API

覆盖 `app/admin_api.py`(~810 行)——控制台背后的全部 REST 端点(25 条路由)、会话鉴权、登录防爆破与网站/告警设置体系。

## 模块结构

```
admin_api.py
├─ 密码工具     _hash_password / _verify_password(PBKDF2-SHA256, 200k 轮)
├─ 展示工具     mask_key(key 脱敏展示)
├─ 加密工具     _fernet(SESSION_SECRET 派生 Fernet,用于 Token 导出)
├─ 会话         SessionManager(签名 Cookie)/ LoginThrottle(防爆破)
├─ 辅助         read_json(容错的 JSON body 解析)
│                _normalize_allowed_tools(白名单归一化:list/CSV → CSV)
└─ build_admin_routes(config) → list[Route](25 条路由)
```

## 会话与鉴权

### SessionManager — 签名 Cookie

| 项 | 值 |
|---|---|
| Cookie 名 | `tpm_admin`(常量在 `config.py`) |
| 值 | `TimestampSigner(session_secret).sign("admin")` |
| 有效期 | 7 天(`SESSION_MAX_AGE = 7 * 86400`) |
| 属性 | `HttpOnly` + `SameSite=lax` + `Secure`(由 `COOKIE_SECURE` 决定) |

无服务端会话存储——签名即凭证,`unsign(max_age=7d)` 通过且内容为 `admin` 即有效。修改 `SESSION_SECRET` 会使所有会话失效。

### LoginThrottle — 登录防爆破

- 按**客户端 IP** 内存计数:连续失败 5 次(`LOGIN_MAX_FAILS`)锁定 10 分钟(`LOGIN_LOCK_SECONDS`)
- 锁定期间返回 429 + 剩余等待秒数;登录成功清零计数
- 进程内存态,重启即清(轻量设计,配合强密码已够用)

### `admin()` 装饰器与错误边界

所有受保护端点包在 `admin(handler)` 里:

- Cookie 无效 → `401 {"error": "unauthorized"}`
- handler 抛任意异常 → 记 `logger.exception` + `500 {"error": "internal error"}`(不泄露堆栈给前端)

## 密码体系(双轨)

| 来源 | 优先级 | 形态 |
|---|---|---|
| `settings.admin_password_hash` | 高 | `salt$digest`(PBKDF2-HMAC-SHA256,200,000 轮) |
| 环境变量 `ADMIN_PASSWORD` | 低(兜底) | 明文,`secrets.compare_digest` 比较 |

首次启动用环境变量登录;在「网站设置」改密码后写入 settings 哈希,**此后环境变量即失效**(改环境变量密码不会影响已改密的实例)。改密接口校验当前密码、要求新密码 ≥8 位。

## Token 的加密导出(`_fernet` / `token_enc`)

访问 Token 明文不落库明文、又要支持控制台"一键导出 MCP 地址",折中方案:

```python
key = base64.urlsafe_b64encode(sha256(SESSION_SECRET).digest())
Fernet(key)   # 确定性派生:同一 SESSION_SECRET 总能解密
```

- **创建**时:`token_enc = fernet.encrypt(明文)` 存入 `access_tokens.token_enc`,同时存 `token_hash`(SHA-256)用于校验
- **导出**(`/reveal`):解密 `token_enc` 返回明文
- 两种不可恢复的情形(接口返回 `token: null` + 中文 reason):
  - 老 Token(导出功能上线前创建,无 `token_enc`)→ 删除重建
  - `SESSION_SECRET` 已变更 → 解密失败(`InvalidToken`)→ 删除重建

## 端点清单(25 条)

公开(无会话):`/api/login`、`/api/logout`、`/api/public-info`、`/site-icon`;其余均需管理会话。

### 会话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | 密码登录;成功签发 Cookie。受 LoginThrottle 保护 |
| POST | `/api/logout` | 清除 Cookie |
| GET | `/api/session` | 会话有效性探测(前端 `RequireAuth` 用) |

### 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 今日请求/成功数、本月请求/Credits、Key 池四态计数、池容量与已用、活跃 Token 数、key 历史总调用。数据源:实时 SQL 聚合 + 内存池快照 |
| GET | `/api/stats/daily?days=N` | 按 UTC 日聚合的请求数/错误数/Credits,`days` 限 1-90(默认 14),喂给概览页趋势图 |

### Key 池

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/keys` | 全量 key 列表:脱敏 key(`mask_key`)、`status`(effective)与 `stored_status`、月度配额进度、last_error 等。数据来自**内存池快照**,不含 last_usage_json |
| POST | `/api/keys` | 批量添加。`keys` 接受多行字符串或数组(先去重),统一 label 与 `plan_limit`(默认 1000);库级 UNIQUE 去重,返回 `{added, skipped_duplicates}` |
| PATCH | `/api/keys/{id}` | `enabled`(启停,清冷却)、`label`、`plan_limit` 任意组合 |
| DELETE | `/api/keys/{id}` | 删除 key(历史日志保留,靠 LEFT JOIN 显示 `-`) |
| POST | `/api/keys/{id}/test` | **测试连接**:真实 `GET /usage`。成功→解析并 `apply_usage` 校准(可能把 exhausted 恢复为 active),返回延迟/plan/remaining/source("account" 表示按账户配额);401→禁用该 key;其他失败→仅记 last_error。不消耗 search credits |
| POST | `/api/keys/sync-all` | **全量校准**:对所有非 disabled key 执行 `sync_all_keys`,返回 `{ok, failed, recovered}`。控制台「全量校准」按钮;付费 key 的真实配额也靠它回填 |

### 访问 Token

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tokens` | 列表 + 每 Token 的今日请求数/本月请求数/本月 Credits(三条聚合查询合并) |
| POST | `/api/tokens` | 创建:名称必填,`tier`(standard/full)、`allowed_tools`(逗号分隔白名单,NULL=不限)、`rpm_limit`(默认取 `DEFAULT_TOKEN_RPM`)、`daily_quota`、`monthly_credits_limit` 可选。生成 `tpm_ + 40 位 hex`,**明文仅此一次返回**;同时存 SHA-256 哈希与 Fernet 密文 |
| PATCH | `/api/tokens/{id}` | 改名/tier/allowed_tools/三项限额;`is_active=false` 时写 `revoked_at`(吊销即时生效——verifier 每请求查库) |
| DELETE | `/api/tokens/{id}` | 物理删除(客户端立即失联) |
| GET | `/api/tokens/{id}/reveal` | 解密导出明文(见上文"加密导出") |

### 日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/logs` | 分页(默认 50,上限 200)+ 筛选(`token_id`/`key_id`/`status`/`tool`,全为精确匹配)。LEFT JOIN 出 token 名称与脱敏 key;含 `query`(查询内容,截 200 字符)与 `client_ip`;返回 `{total, items}` |

### 设置与公开信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/public-info` | **公开**:站名/公告/公告更新时间。登录页与 MCP `get_my_usage` 共用 |
| GET | `/site-icon` | **公开**:站点图标。自定义图标来自 `DATA_DIR/site_icon.bin`(Content-Type 存 settings,缓存 5 分钟),否则回退构建产物里的 `favicon.png`(缓存 1 天) |
| GET | `/api/settings` | 管理侧读取:站名/公告/`has_custom_icon`/告警配置(渠道、webhook、邮件 SMTP 字段、事件开关、两个阈值) |
| PUT | `/api/settings` | 更新站名(≤40 字符,空则回退默认)与公告(≤2000 字符);公告变更会写 `announcement_updated_at`(前端据此重新弹横幅)。同一请求可携带 `alert_*` 字段更新告警配置(渠道值校验白名单),写完使 Alerter 缓存失效 |
| POST | `/api/settings/alert-test` | 直发一条测试告警到已配置的渠道(webhook 或邮件),同步返回 `{ok, error}`(缺字段时逐项列出),供设置页「发送测试告警」按钮 |
| POST | `/api/settings/icon` | 上传图标:请求体即文件字节,Content-Type 白名单(PNG/JPEG/SVG/WebP/GIF/ICO),≤1MB;写入 `DATA_DIR/site_icon.bin` |
| DELETE | `/api/settings/icon` | 删除自定义图标,恢复默认 |
| POST | `/api/settings/password` | 修改管理员密码(校验当前密码,新密码 ≥8 位) |

## 关键实现细节

- **`read_json`**:body 不是 JSON 对象时返回 `{}`,所有端点拿到的都是 dict,天然防脏输入
- **`mask_key`**:≤12 字符显示前 4 位,否则 `前8…后4`,日志页与 Key 列表共用
- **路由注册**:返回 `list[Route]`,由 `main.create_app()` `extend` 进 `http_app()` 的 routes——与 MCP 端点同居一个 Starlette 应用
- **审计**:管理端没有操作日志,敏感动作(改密/吊销)只进 Python logger,不上数据库
