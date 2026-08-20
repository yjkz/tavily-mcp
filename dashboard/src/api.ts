export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers ?? {}),
    },
  })
  if (resp.status === 401 && !path.startsWith('/api/login')) {
    window.location.hash = '#/login'
    throw new ApiError(401, '请先登录')
  }
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new ApiError(resp.status, (data as { error?: string }).error ?? `请求失败 (${resp.status})`)
  }
  return data as T
}

// ---- types ----

export interface KeyItem {
  id: number
  label: string
  masked_key: string
  status: 'active' | 'cooling' | 'exhausted' | 'disabled'
  stored_status: string
  cooldown_until: number | null
  credits_used_month: number
  plan_limit: number
  remaining_credits: number
  monthly_reset_at: number | null
  total_requests: number
  last_used_at: number | null
  last_error: string | null
}

export interface KeyTestResult {
  ok: boolean
  latency_ms?: number
  status?: number
  error?: string
  plan?: string | null
  source?: 'key' | 'account'
  calibrated?: boolean
  recovered?: boolean
  credits_used?: number
  plan_limit?: number
  remaining?: number | null
}

export interface TokenItem {
  id: number
  name: string
  prefix: string
  tier: 'standard' | 'full'
  allowed_tools: string | null
  rpm_limit: number
  daily_quota: number | null
  monthly_credits_limit: number | null
  is_active: boolean
  last_used_at: number | null
  created_at: number
  today_requests: number
  month_requests: number
  month_credits: number
}

export interface CreatedToken {
  id: number
  name: string
  tier: string
  token: string
}

export interface LogItem {
  id: number
  ts: number
  token_name: string
  tool: string
  query: string | null
  tavily_key: string
  status: string
  http_status: number | null
  credits: number
  latency_ms: number | null
  error_detail: string | null
  request_id: string | null
  client_ip: string
}

export interface PublicInfo {
  site_name: string
  announcement: string | null
  announcement_updated_at: number | null
}

export interface AlertConfig {
  channel: string
  webhook_url: string
  webhook_secret: string
  email_smtp_host: string
  email_smtp_port: number
  email_smtp_ssl: boolean
  email_username: string
  email_password: string
  email_from: string
  email_to: string
  on_key_disabled: boolean
  on_key_exhausted: boolean
  on_pool_exhausted: boolean
  pool_min_active: number
  pool_min_remaining: number
}

export interface SiteSettings {
  site_name: string
  announcement: string
  announcement_updated_at: number | null
  has_custom_icon: boolean
  alert: AlertConfig
}

export interface LogsPage {
  total: number
  items: LogItem[]
}

export interface Overview {
  today_requests: number
  today_success: number
  month_requests: number
  month_credits: number
  keys_total: number
  keys_status: { active: number; cooling: number; exhausted: number; disabled: number }
  pool_capacity_limit: number
  pool_credits_used: number
  tokens_active: number
  total_key_requests: number
}

export interface DailyStat {
  date: string
  requests: number
  errors: number
  credits: number
}

// ---- api ----

export const api = {
  login: (password: string) =>
    request<{ ok: boolean }>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/logout', { method: 'POST' }),
  session: () => request<{ ok: boolean }>('/api/session'),
  overview: () => request<Overview>('/api/overview'),
  dailyStats: (days = 14) => request<DailyStat[]>(`/api/stats/daily?days=${days}`),
  keys: () => request<KeyItem[]>('/api/keys'),
  addKeys: (payload: { keys: string; label: string; plan_limit: number }) =>
    request<{ added: number; skipped_duplicates: number }>('/api/keys', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  patchKey: (id: number, payload: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/api/keys/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteKey: (id: number) => request<{ ok: boolean }>(`/api/keys/${id}`, { method: 'DELETE' }),
  testKey: (id: number) =>
    request<KeyTestResult>(`/api/keys/${id}/test`, { method: 'POST' }),
  syncAllKeys: () =>
    request<{ ok: number; failed: number; recovered: number }>('/api/keys/sync-all', {
      method: 'POST',
    }),
  tokens: () => request<TokenItem[]>('/api/tokens'),
  createToken: (payload: Record<string, unknown>) =>
    request<CreatedToken>('/api/tokens', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  patchToken: (id: number, payload: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/api/tokens/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteToken: (id: number) => request<{ ok: boolean }>(`/api/tokens/${id}`, { method: 'DELETE' }),
  revealToken: (id: number) =>
    request<{ token: string | null; reason?: string }>(`/api/tokens/${id}/reveal`),
  publicInfo: () => request<PublicInfo>('/api/public-info'),
  getSettings: () => request<SiteSettings>('/api/settings'),
  updateSettings: (payload: {
    site_name?: string
    announcement?: string
    alert_channel?: string
    alert_webhook_url?: string
    alert_webhook_secret?: string
    alert_email_smtp_host?: string
    alert_email_smtp_port?: number
    alert_email_smtp_ssl?: boolean
    alert_email_username?: string
    alert_email_password?: string
    alert_email_from?: string
    alert_email_to?: string
    alert_on_key_disabled?: boolean
    alert_on_key_exhausted?: boolean
    alert_on_pool_exhausted?: boolean
    alert_pool_min_active?: number
    alert_pool_min_remaining?: number
  }) =>
    request<{ ok: boolean }>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  alertTest: () =>
    request<{ ok: boolean; error: string | null }>('/api/settings/alert-test', {
      method: 'POST',
    }),
  uploadIcon: async (file: File) => {
    const resp = await fetch('/api/settings/icon', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      throw new ApiError(resp.status, (data as { error?: string }).error ?? '上传失败')
    }
    return data as { ok: boolean; size: number }
  },
  deleteIcon: () => request<{ ok: boolean }>('/api/settings/icon', { method: 'DELETE' }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean }>('/api/settings/password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),
  logs: (params: { limit?: number; offset?: number; token_id?: string; status?: string; tool?: string }) => {
    const q = new URLSearchParams()
    if (params.limit) q.set('limit', String(params.limit))
    if (params.offset) q.set('offset', String(params.offset))
    if (params.token_id) q.set('token_id', params.token_id)
    if (params.status) q.set('status', params.status)
    if (params.tool) q.set('tool', params.tool)
    return request<LogsPage>(`/api/logs?${q.toString()}`)
  },
}

// ---- helpers ----

export function formatTs(ts: number | null | undefined): string {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}
