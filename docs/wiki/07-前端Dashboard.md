# 07 · 前端 Dashboard

`dashboard/` 下的 React SPA,中文界面、默认深色主题,构建产物由后端 `StaticFiles` 托管在 `/`。

## 技术栈

| 层 | 选型 |
|---|---|
| 框架 | React 19 + TypeScript(`strict`,经 `tsc -b` 构建期校验) |
| 路由 | react-router-dom 7,**HashRouter** |
| 构建 | Vite 8(`@vitejs/plugin-react`) |
| 样式 | Tailwind CSS v4(`@tailwindcss/vite` 插件,无 tailwind.config,样式即代码)+ `tw-animate-css` |
| 组件 | shadcn/ui(radix-ui 统一包)+ lucide-react 图标 |
| 图表 | recharts 3 |
| 通知 | sonner |
| Lint | oxlint(`npm run lint`) |

## 目录结构

```
dashboard/
├─ index.html                 # Vite 入口(引用 /site-icon 作 favicon)
├─ vite.config.ts             # react + tailwindcss 插件;'@' → src 别名;dev 代理
├─ src/
│  ├─ main.tsx                # createRoot + StrictMode
│  ├─ App.tsx                 # HashRouter 路由表 + RequireAuth 守卫
│  ├─ api.ts                  # 类型化 API 客户端(唯一的后端通信层)
│  ├─ index.css               # Tailwind 主题变量(深色为默认)
│  ├─ components/
│  │  ├─ Layout.tsx           # 侧边栏 + 公告横幅 + <Outlet/>
│  │  └─ ui/                  # shadcn/ui 生成的基础组件(20 个)
│  └─ pages/
│     ├─ Login.tsx  Overview.tsx  Keys.tsx
│     ├─ Tokens.tsx  Logs.tsx  Settings.tsx
```

## 路由与鉴权

`App.tsx` 的路由表:

| 路径 | 页面 | 守卫 |
|---|---|---|
| `#/login` | Login | 无 |
| `#/`(index) | Overview | RequireAuth |
| `#/keys` | Keys | RequireAuth |
| `#/tokens` | Tokens | RequireAuth |
| `#/logs` | Logs | RequireAuth |
| `#/settings` | Settings | RequireAuth |
| `*` | 重定向 `#/` | — |

**双层 401 拦截**:

1. `RequireAuth` 组件:进入受保护路由前调 `api.session()`,失败跳 `#/login`(展示 Skeleton 过渡)
2. `api.ts` 的 `request()`:任何接口返回 401 且非登录接口 → 强制跳 `#/login`(会话过期兜底)

**为什么 HashRouter**:静态托管只有一条兜底路由(`/` 返回 `index.html`),Hash 路由不依赖服务端 SPA fallback,刷新任意页面都可用;代价是 URL 带 `#`,对本控制台无伤。

## api.ts — 类型化 API 客户端

前端唯一的后端通信层,与后端端点一一对应(契约见 [05](05-管理API.md)):

- **`request<T>(path, options)`** 通用封装:`credentials: 'same-origin'` 携带会话 Cookie;有 body 时自动加 JSON 头;非 2xx 抛 `ApiError(status, 后端 error 文案)`;401 全局跳登录
- **类型定义**:`KeyItem` / `TokenItem`(含 `allowed_tools`)/ `LogItem`(含 `query`)/ `Overview` / `DailyStat` / `SiteSettings`(含 `alert: AlertConfig`)/ `PublicInfo` / `KeyTestResult` / `CreatedToken` 等,字段与后端 JSON 逐一对齐
- **`api` 对象**:约 28 个方法(login/logout/session/overview/dailyStats/keys CRUD+test+**syncAllKeys**/tokens CRUD+reveal/logs/设置/图标/改密/**alertTest**)
- **特例 `uploadIcon`**:图标上传是原始字节 body(非 JSON),单独用 fetch 实现,Content-Type 取 `file.type`
- **`formatTs`**:Unix 秒 → `zh-CN` 本地时间字符串

## Layout — 布局与公告

- 左侧固定 56 侧边栏:logo(拉取 `/site-icon`)+ 站名(来自 `/api/public-info`)+ 五个 NavLink + 退出登录
- `document.title` 动态设为 `{site_name} 控制台`
- **公告横幅(`AnnouncementBanner`)**:
  - 数据来自 `/api/public-info`(公开接口)
  - 用户点 X 关闭后写 `localStorage['tpm_announcement_dismissed'] = announcement_updated_at`
  - 公告内容更新 → `updated_at` 变化 → 横幅重新出现(按"版本号"记忆,而非永久关闭)

## 页面功能速览

### Login

密码单字段登录;加载 `/api/public-info` 显示站名/公告/自定义图标(品牌可定制的一部分);错误信息直接展示后端文案(如"失败次数过多,请 N 秒后再试")。

### Overview(概览)

- 4 张 StatCard:今日请求(成功数)/ 本月 Credits / Key 池健康(active/total + 四态圆点)/ 活跃 Token
- 近 14 天趋势:`ComposedChart`——请求/错误双柱 + Credits 折线
- Key 池本月配额:全池 `已用/总容量` 进度条

### Keys(Key 池)

- `KeyCard` 网格卡片:状态徽章、脱敏 key、月度配额进度条、总调用/最后使用、`last_error` 提示
- **测试连接**:`POST /api/keys/{id}/test`,toast 拼装展示 延迟/剩余 credits/计划/「按账户配额」/「已自动恢复」
- **全量校准**:`POST /api/keys/sync-all`,对所有 key 批量跑一次 `/usage` 校准,toast 展示 成功/失败/恢复数——免等 6h 周期,付费 key 配额录错也能立即修正
- 启用/禁用切换、AlertDialog 确认删除
- 批量添加对话框:多行 textarea 粘贴 key + 共享备注 + 月度配额,后端去重后返回 added/skipped

### Tokens(访问 Token)

- 表格视图:名称/前缀/等级(完整|基本)/RPM/日配额/月 Credits/今日请求/本月 Credits/最后使用/启用开关/操作
- **导出按钮**:先 `reveal` 拿明文,再拼 `{origin}/mcp?token=xxx` 写剪贴板(一键产出可直接填入 MCP 客户端的地址);旁边的小钥匙按钮只复制明文
- 创建对话框:名称/tier/RPM/日配额/月 Credits + **允许的工具**(逗号分隔白名单,留空不限;`get_my_usage` 始终可用,等级门禁仍然生效);创建成功弹出**一次性明文展示**对话框(CopyRow 组件),关掉即不可再见(除非导出)
- 删除走 AlertDialog 确认

### Logs(请求日志)

- 筛选器:Token(下拉,选项来自 `/api/tokens`)/ 状态 / 工具(含 `tavily_research`),均为精确匹配
- **查询内容列**:`log.query`(后端截 200 字符的 query/URL/研究主题),悬停 title 看全文——审计"用户在搜什么"的主入口
- 分页:固定 `PAGE_SIZE = 50`,`?offset=page*50`,显示总数与页码翻页
- 六种 status 的中文标签与配色映射(成功/上游错误/池耗尽/限流/配额超限/等级不足)

### Settings(网站设置)

- 站名 + 公告编辑保存(`PUT /api/settings`,保存后 toast 提示生效位置)
- 图标上传(原生 file input → 字节直传)/ 恢复默认;用 `iconVersion` 计数器给 `<img src="/site-icon?v=N">` 破缓存
- 修改管理员密码(当前 + 新 + 确认,前端先校验 ≥8 位)
- **告警通知卡片**(页面底部整宽):
  - 渠道下拉:关闭 / **邮件(SMTP)** / 飞书 / 企业微信 / 钉钉 / 通用 Webhook
  - 选 Webhook 类渠道显示 URL + 签名密钥;选邮件显示 SMTP 服务器/端口/SSL 开关/发信邮箱/授权码(password 输入)/发件人(可选)/收件人(逗号分隔,支持多个),并提示 163/QQ 填授权码、465+SSL / 587+STARTTLS 组合
  - 三个事件开关(单个 key 禁用 / 单个 key 耗尽 / 全池不可用)+ 两个阈值(可用 key 数 / 剩余 credits,0=关)
  - 「保存告警配置」与「发送测试告警」(测试按钮按渠道就绪状态启用;失败时 toast 展示后端给出的缺失字段提示)

## 开发与构建工作流

```bash
cd dashboard
npm install

npm run dev        # Vite dev server(http://localhost:5173)
                   # /api 与 /mcp 代理到 http://127.0.0.1:8000(vite.config.ts)
                   # 配合后端 TAVILY_POOL_DEV=1 uv run uvicorn app.main:app

npm run build      # tsc -b && vite build → dist/(产物被后端 STATIC_DIR 托管)
npm run lint       # oxlint
npm run preview    # 本地预览构建产物
```

开发期前后端**分离运行**:前端在 5173、后端在 8000,dev 代理解决跨域;Cookie 依赖 `COOKIE_SECURE=0`(dev 模式默认)。

## 与后端的契约要点

- 所有管理接口以 `/api` 开头,登录态靠 `tpm_admin` Cookie,**没有任何 token 存储在 localStorage**(除公告关闭标记)
- 时间戳一律 Unix 秒,由 `formatTs` 本地化
- 后端错误响应统一为 `{"error": "中文文案"}`,前端 toast 直接展示
- Key/Token 的敏感值(key 全文、token 明文)**永不**下发到列表接口,列表只有脱敏形式
