# 04 · MCP 服务层

覆盖 `app/mcp_server.py`(~920 行)——请求链路的核心:鉴权、限流配额中间件、池化执行器与 6 个 MCP 工具全部在此。

## 模块总览

```
mcp_server.py
├─ 常量           TOKEN_PREFIX / FULL_TIER_TOOLS / READ_ONLY_ANNOTATIONS
│                RESEARCH_POLL_INTERVAL / RESEARCH_TIMEOUTS(异步 research 轮询)
├─ 鉴权           QueryTokenAuthMiddleware(?token= 提升)
│                DbTokenVerifier(SHA-256 查库 → AccessToken)
├─ 策略中间件     get_client_ip / log_request / RateLimiter / GatewayMiddleware
├─ 池化执行器     _submit_with_failover(提交 + 故障转移,run/run_research 共用)
│                run(同步端点) / run_research(异步任务,固定 key 轮询)
└─ 工具层         5 个 *Input 模型 + clamp_output + 5 个 format_* + build_mcp()
```

一条 `tools/call` 请求的完整链路(与 [02 的时序图](02-整体架构.md) 对应):

```
QueryTokenAuthMiddleware        # ?token= → Authorization: Bearer(最外层,纯 ASGI)
  └─ FastMCP AuthenticationMiddleware   # 读 Authorization → DbTokenVerifier.verify_token
      └─ GatewayMiddleware.on_call_tool # tier 门禁 → RPM → 日配额 → 月 Credits
          └─ 工具函数                    # tavily_search / …
              └─ PooledExecutor.run     # acquire → 上游调用 → report_* → log_request
```

## 常量

| 常量 | 值 | 用途 |
|---|---|---|
| `TOKEN_PREFIX` | `"tpm_"` | 访问 Token 前缀;verifier 先查前缀,非 `tpm_` 直接拒收(也供 admin_api 复用) |
| `FULL_TIER_TOOLS` | `{"tavily_crawl", "tavily_map", "tavily_research"}` | 高消耗工具,仅 `full` 等级 Token 可调用 |
| `RESEARCH_POLL_INTERVAL` | `5.0` 秒 | research 任务状态轮询间隔(与官方 MCP 一致) |
| `RESEARCH_TIMEOUTS` | mini 120s / pro 300s / auto 300s | research 总超时(与官方 MCP 一致) |
| `READ_ONLY_ANNOTATIONS` | readOnly/destructive/idempotent/openWorld 均为只读语义 | MCP 工具注解,帮助客户端决定是否需要用户确认 |

## QueryTokenAuthMiddleware — URL Token 提升

纯 ASGI 中间件(不继承 Starlette 的 `BaseHTTPMiddleware`),让客户端只靠 URL 就能完成鉴权:

```
https://your-domain/mcp?token=tpm_xxx
```

逻辑(`__call__`):

1. 仅处理 `scope["type"] == "http"` 的请求
2. 解析 `query_string` 中的 `token` 参数;**已有 Authorization 头则不动**(显式头优先)
3. 注入 `Authorization: Bearer <token>`,通过 `scope = {**scope, "headers": headers}` 复制新 scope,避免污染原对象

> **为什么必须包在整个 app 最外层**:FastMCP 的 `AuthenticationMiddleware` 位于用户通过 `http_app(middleware=[...])` 注入的中间件**外层**,会先一步读取 Authorization 头。实测该注入方式无效,因此此中间件在 `main.create_app()` 末尾手动包裹整个应用(见 [02](02-整体架构.md))。这与 Tavily 官方托管 MCP 的 `?tavilyApiKey=` 形式同款思路。

## DbTokenVerifier — Token 校验

继承 `fastmcp.server.auth.auth.TokenVerifier`,实现 `verify_token(token) -> AccessToken | None`:

```
token 不以 tpm_ 开头 → None
SHA-256(token) 查 access_tokens.token_hash
  无记录 或 is_active=0 → None(吊销即时生效:每次请求实时查库)
  命中 → UPDATE last_used_at(每次验证都写库,换取「最后使用」统计)
       → AccessToken(claims={token_id, name, tier, rpm_limit,
                            daily_quota, monthly_credits_limit})
```

- `claims` 是下游一切策略的输入:GatewayMiddleware 用它做门禁/限额,PooledExecutor 用 `token_id` 记日志
- `scopes=["tavily", "tier:<tier>"]` 仅作形式化声明,本项目不按 scope 鉴权
- `expires_at=None`:Token 永不过期,生命周期完全由管理员控制

## GatewayMiddleware — 策略中间件

`on_call_tool` 按以下顺序**短路**检查,每一步拒绝都写一条 `request_logs`(可审计)并抛出面向终端用户可读的 `ToolError`:

| # | 检查 | 条件 | 日志 status | 说明 |
|---|---|---|---|---|
| 0 | 无 Token 上下文 | `get_access_token()` 为 None | — | 直接放行;HTTP 传输总会先鉴权,此分支只出现在进程内(stdio/memory)调用 |
| 1 | tier 门禁 | 工具 ∈ `FULL_TIER_TOOLS` 且 `tier != "full"` | `tier_denied` | crawl/map/research 消耗大,提示改用 search/extract |
| 1b | 工具白名单 | `allowed_tools` 非 NULL 且工具不在名单(排除 `get_my_usage`) | `tier_denied` | 按 Token 的细粒度开关;`get_my_usage` 自查永远放行 |
| 2 | RPM 限流 | `RateLimiter.allow(token_id, rpm_limit)` 为 False | `rate_limited` | 滑动窗口,默认 30 次/分钟 |
| 3 | 日请求配额 | 当日(UTC)`COUNT(request_logs) >= daily_quota`(排除 `rate_limited` 状态) | `quota_exceeded` | `daily_quota` 为 NULL 时不限 |
| 4 | 月 Credits 上限 | 当月(UTC)`TOTAL(credits) >= monthly_credits_limit`(仅 `success`) | `quota_exceeded` | 为 NULL 时不限 |

统计口径细节:

- **日配额排除 `rate_limited`**:被限流的尝试不计入配额,防止"越限越罚"
- **月 Credits 只统计 `success`**:失败请求不产生上游消耗
- 日/月统计是对 `request_logs` 的实时 SQL 聚合,依赖 `(token_id, ts)` 索引;没有单独的计数器表

### RateLimiter — 进程内滑动窗口

```python
self._windows: dict[int, deque[float]] = defaultdict(deque)

def allow(self, token_id, limit):
    now = time.monotonic()
    window = self._windows[token_id]
    while window and window[0] <= now - 60.0:   # 弹出窗口外的时间戳
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True
```

- **内存态、不持久化、重启清零**——有意的轻量设计;配额的"硬约束"由日/月配额(落库)承担
- 按 `token_id` 隔离,不同 Token 互不影响
- 用 `time.monotonic()` 而非墙钟,不受系统时间跳变影响

### 辅助函数

| 函数 | 说明 |
|---|---|
| `get_client_ip()` | 取 `get_http_request()`(FastMCP 上下文依赖);优先 `X-Forwarded-For` 首跳(可信 Nginx 注入),回退 socket 对端;取不到返回 None。端口不直接暴露公网,直连伪造 XFF 只影响日志 |
| `_query_summary(endpoint, payload)` | 为日志提取"查了什么":search→query、extract→前 3 个 URL、crawl/map→url、research→input,统一截 200 字符 |
| `log_request()` | 全字段 INSERT 进 `request_logs`(含 query 列),同时递增 Prometheus 计数器(`tpm_requests_total{tool,status}`、成功时 `tpm_credits_total`),见 [06](06-数据模型.md) |

## PooledExecutor — 池化执行与故障转移

### `_submit_with_failover(call, tool, payload)`

`run()` 与 `run_research()` 共用的提交内核:acquire → 调用上游 → 按错误类型处置 → 成功返回 `(key, 响应, 单次耗时)`。重试策略:

| 上游结果 | 池动作(KeyPool) | 是否换 key 重试 | request_logs |
|---|---|---|---|
| 200 / 201 | 由调用方记 success | — | — |
| 429 | `report_rate_limited`(冷却 60s) | 是 | — |
| 432 / 433 | `report_exhausted`(标记耗尽) | 是 | — |
| 401 | `report_invalid`(禁用) | 是 | — |
| 5xx / 0(网络) | `report_transient`(只记 last_error) | 是 | — |
| 其余 4xx | `report_transient` | **否** | `upstream_error` + 立即抛 `ToolError` |

关键设计:

- **4xx 参数错误不重试**:坏请求换任何 key 都会失败,直接透传并提示"修正参数后重试",省 credits
- **重试上限**:`max_attempts = min(len(pool), config.max_retries)`(默认 4)——池比上限小时恰好一轮遍历
- **结束态**:`attempts == 0`(全池不可用)→ `pool_exhausted` + `next_recovery_hint()`;重试耗尽 → `upstream_error`,error_detail 汇总**最近 3 条**失败原因
- 429/432/433/401/5xx 的中间尝试**不单独记日志**,只有最终结果落一条——日志表不被重试放大

### `run(endpoint, payload)` — 同步端点

search / extract / crawl / map 走这里:`_submit_with_failover` 拿到 `(ks, data, latency_ms)` 后,解析 `usage.credits` → `report_success` → 记 `success` 日志 → 返回响应。

### `run_research(params)` — 异步深度研究

research 与其余端点有本质区别:上游 `POST /research` 返回 `201 + {request_id, status:"pending"}`,需要轮询 `GET /research/{request_id}` 直到 `completed`,整个过程可能持续数分钟。因此它**不能用**"一次调用 = 一个 key"的同步模型:

```
阶段 1 提交:_submit_with_failover(self.tavily.research, …)
           可换 key 重试(任务尚未创建,429/432/401 照常故障转移)
阶段 2 轮询:固定在创建任务的 key 上(任务钉死在该 key 的上游账户里,换 key 即换账户,查不到任务)
           while status != "completed":
               status == "failed"     → upstream_error + ToolError
               超过 RESEARCH_TIMEOUTS[model] → upstream_error(带 request_id)+ ToolError
               sleep(RESEARCH_POLL_INTERVAL)
               GET /research/{request_id}
                   401                → report_invalid + upstream_error + ToolError(任务无法继续)
                   其他瞬时错误        → 忽略,继续轮询直到超时
```

- **超时与轮询节奏对齐官方 MCP**:`RESEARCH_POLL_INTERVAL = 5s`;`RESEARCH_TIMEOUTS`:mini 120s、pro/auto 300s
- **轮询期间的 429/5xx/网络错误不惩罚 key**:只重试(该 key 已被任务占用,冷却也不影响进行中的轮询)
- **POST 响应直接 completed**(上游偶尔同步完成)则跳过轮询
- **credits**:完成响应里的 `usage.credits`(如有)计入 `report_success`;缺省记 0,真实消耗由 6h usage-sync 校准
- **日志**:整条任务只记一条 `request_logs`(tool=`tavily_research`,`latency_ms` 为提交到完成的总耗时,`request_id` 为上游任务 ID)

## 工具层

`build_mcp(state, lifespan)` 组装 FastMCP 实例:

```python
FastMCP(
    name="tavily_pool_mcp",
    lifespan=lifespan,                    # 由 main.create_app 注入,统一生命周期
    instructions="…工具选择建议…",         # 客户端可见的服务器说明
    auth=verifier,                        # DbTokenVerifier
    middleware=[GatewayMiddleware(state.db)],
)
```

### 6 个工具

| 工具 | 入参模型 | 要点 | 输出 |
|---|---|---|---|
| `tavily_search` | `SearchInput` | 默认 `include_answer=True`;`topic='news'` 配 `days=N` | `format_search` → Markdown(Answer + 编号结果) |
| `tavily_extract` | `ExtractInput` | 1-20 个 URL;失败 URL 以 `[FAILED]` 列出 | `format_extract` → 每域名一节 |
| `tavily_crawl` | `CrawlInput` | **full 等级**;每页内容截 2000 字符 | `format_crawl` → 按面包屑路径分组 |
| `tavily_map` | `MapInput` | **full 等级**;最便宜的结构发现(~1 credit/50 URL) | `format_map` → 每行一个 URL |
| `tavily_research` | `ResearchInput` | **full 等级**;异步深度研究:提交后固定 key 轮询,最长数分钟 | `format_research` → 报告正文 + 来源清单 |
| `get_my_usage` | 无 | 客户端自查:今日请求/本月 Credits/各限额;附带 `site_name` 与 `announcement`(运营触达通道) | JSON 字符串 |

### 入参模型(pydantic,即工具的 JSON Schema)

| 模型 | 字段摘录 |
|---|---|
| `SearchInput` | `query`(1-400 字符)、`search_depth`(basic/advanced)、`topic`(general/news/finance)、`max_results`(1-20)、`days`(仅 news)、`time_range`、`include_answer`、`include_raw_content`、`include/exclude_domains`(各 ≤20) |
| `ExtractInput` | `urls`(1-20)、`extract_depth`、`format`(markdown/text) |
| `CrawlInput` | `url`、`max_depth`(1-5)、`limit`(1-100)、`instructions`(≤500 字符,自然语言过滤) |
| `MapInput` | `url`、`max_depth`(1-5)、`limit`(1-500) |
| `ResearchInput` | `input`(研究问题,≤2000 字符)、`model`(mini/pro/auto)、`output_length`(short/standard/long)、`citation_format`(numbered/mla/apa/chicago)、`include/exclude_domains`(各 ≤20)。不暴露 `stream`/`output_schema`/`files` 等高级参数 |

每个模型的 `payload()` 方法把字段转成上游请求体;`None` 的可选字段不发送(用上游默认值)。search/extract/crawl/map 会统一附加 `include_usage: True`;research 的 `payload()` 只发 `input`/`model` 加可选过滤项(上游 /research 无此参数)。

### 输出格式化与截断

| 函数 | 说明 |
|---|---|
| `clamp_output(text, limit)` | 超过 `character_limit`(默认 25,000)时截断,并追加自解释提示(引导收窄参数) |
| `format_search` | `Answer:` 摘要 + 编号结果(标题/URL/相关性/发布时间/内容,可选 raw_content) |
| `format_extract` | 每个 URL 一节 + `[FAILED]` 失败清单 |
| `format_crawl` | 面包屑路径(`a > b > c`)作标题,单页内容截 2000 字符 |
| `format_map` | 纯 URL 列表 |
| `format_research` | 报告正文(markdown)+ `## Sources` 编号来源清单 |

工具 docstring 写明了使用时机与错误处置建议(4xx 改参数、限流稍后重试),这些文字会进入客户端 LLM 的上下文,是调优"模型会不会正确用工具"的低成本杠杆。
