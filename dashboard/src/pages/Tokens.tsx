import { useCallback, useEffect, useState } from 'react'
import { Check, Copy, KeyRound, Link2, Plus, RefreshCcw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api, formatTs, type CreatedToken, type TokenItem } from '@/api'

function CopyRow({
  label,
  hint,
  value,
  copied,
  onCopy,
}: {
  label: string
  hint?: string
  value: string
  copied: boolean
  onCopy: () => void | Promise<void>
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-foreground">{label}</span>
        {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
      </div>
      <div className="flex items-start gap-2 rounded-lg border bg-muted/50 p-3">
        <code className="min-w-0 flex-1 break-all font-mono text-xs leading-relaxed">{value}</code>
        <Button size="sm" variant="outline" className="shrink-0" onClick={onCopy}>
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? '已复制' : '复制'}
        </Button>
      </div>
    </div>
  )
}

export default function TokensPage() {
  const [tokens, setTokens] = useState<TokenItem[] | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [form, setForm] = useState({ name: '', tier: 'standard', rpm: '30', daily: '', monthly: '', tools: '' })
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<CreatedToken | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api
      .tokens()
      .then(setTokens)
      .catch((e) => toast.error(e instanceof Error ? e.message : '加载失败'))
  }, [])

  useEffect(refresh, [refresh])

  const handleCreate = async () => {
    if (!form.name.trim()) return
    setCreating(true)
    try {
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        tier: form.tier,
        rpm_limit: Number(form.rpm) || 30,
      }
      if (form.daily.trim()) payload.daily_quota = Number(form.daily)
      if (form.monthly.trim()) payload.monthly_credits_limit = Number(form.monthly)
      if (form.tools.trim()) payload.allowed_tools = form.tools.trim()
      const result = await api.createToken(payload)
      setCreateOpen(false)
      setCreated(result)
      setCopied(null)
      setForm({ name: '', tier: 'standard', rpm: '30', daily: '', monthly: '', tools: '' })
      refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  const handleToggle = async (item: TokenItem, active: boolean) => {
    await api.patchToken(item.id, { is_active: active }).catch((e) =>
      toast.error(e instanceof Error ? e.message : '操作失败'),
    )
    refresh()
  }

  const handleDelete = async (item: TokenItem) => {
    await api.deleteToken(item.id).catch((e) => toast.error(e instanceof Error ? e.message : '删除失败'))
    toast.success('已删除')
    refresh()
  }

  const handleExport = async (item: TokenItem, what: 'url' | 'token') => {
    const revealed = await api.revealToken(item.id).catch(() => null)
    const plaintext = revealed?.token
    if (!plaintext) {
      toast.error(revealed?.reason ?? '无法导出该 Token')
      return
    }
    const value =
      what === 'url'
        ? `${window.location.origin}/mcp?token=${plaintext}`
        : plaintext
    await navigator.clipboard.writeText(value)
    toast.success(what === 'url' ? '已复制 MCP 地址,可直接填入客户端' : '已复制 Token')
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">访问 Token</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            签发给 MCP 客户端的 Bearer Token,可独立限流、配额与吊销
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={refresh}>
            <RefreshCcw className="size-4" />
            刷新
          </Button>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />
            创建 Token
          </Button>
        </div>
      </div>

      {!tokens ? (
        <Skeleton className="h-64" />
      ) : tokens.length === 0 ? (
        <div className="rounded-xl border border-dashed p-12 text-center text-sm text-muted-foreground">
          还没有 Token。创建一个,然后把它配置到 MCP 客户端(Authorization: Bearer …)。
        </div>
      ) : (
        <div className="rounded-xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>前缀</TableHead>
                <TableHead>等级</TableHead>
                <TableHead className="text-right">RPM</TableHead>
                <TableHead className="text-right">日配额</TableHead>
                <TableHead className="text-right">月 Credits</TableHead>
                <TableHead className="text-right">今日请求</TableHead>
                <TableHead className="text-right">本月 Credits</TableHead>
                <TableHead>最后使用</TableHead>
                <TableHead>启用</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {tokens.map((t) => (
                <TableRow key={t.id} className={t.is_active ? '' : 'opacity-50'}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{t.prefix}…</TableCell>
                  <TableCell>
                    <Badge variant={t.tier === 'full' ? 'default' : 'secondary'}>
                      {t.tier === 'full' ? '完整' : '基本'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{t.rpm_limit}</TableCell>
                  <TableCell className="text-right tabular-nums">{t.daily_quota ?? '不限'}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {t.monthly_credits_limit ?? '不限'}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{t.today_requests}</TableCell>
                  <TableCell className="text-right tabular-nums">{t.month_credits}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{formatTs(t.last_used_at)}</TableCell>
                  <TableCell>
                    <Switch checked={t.is_active} onCheckedChange={(v) => handleToggle(t, v)} />
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-muted-foreground"
                        title="一键复制 MCP 地址(带 Token,直接填入客户端)"
                        onClick={() => handleExport(t, 'url')}
                      >
                        <Link2 className="size-4" />
                        导出
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground"
                        title="复制 Token 明文"
                        onClick={() => handleExport(t, 'token')}
                      >
                        <KeyRound className="size-4" />
                      </Button>
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button variant="ghost" size="icon" className="text-destructive hover:text-destructive">
                            <Trash2 className="size-4" />
                          </Button>
                        </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle className="break-words">删除 Token「{t.name}」?</AlertDialogTitle>
                          <AlertDialogDescription>
                            将删除 {t.prefix}…(「{t.name}」)。删除后使用该 Token
                            的所有客户端将立即无法访问,此操作不可撤销。
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>取消</AlertDialogCancel>
                          <AlertDialogAction onClick={() => handleDelete(t)}>删除</AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>创建访问 Token</DialogTitle>
            <DialogDescription>
              明文 Token 只在创建后显示一次。「完整」等级才能调用 crawl / map / research。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="tname">名称</Label>
              <Input
                id="tname"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如:我的 Claude Code"
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label>等级</Label>
                <Select value={form.tier} onValueChange={(v) => setForm({ ...form, tier: v })}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="standard">基本(search/extract)</SelectItem>
                    <SelectItem value="full">完整(含 crawl/map)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="trpm">每分钟限额 (RPM)</Label>
                <Input
                  id="trpm"
                  type="number"
                  value={form.rpm}
                  onChange={(e) => setForm({ ...form, rpm: e.target.value })}
                />
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="tdaily">日请求配额(可选)</Label>
                <Input
                  id="tdaily"
                  type="number"
                  placeholder="不限"
                  value={form.daily}
                  onChange={(e) => setForm({ ...form, daily: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="tmonthly">月 Credits 上限(可选)</Label>
                <Input
                  id="tmonthly"
                  type="number"
                  placeholder="不限"
                  value={form.monthly}
                  onChange={(e) => setForm({ ...form, monthly: e.target.value })}
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="ttools">允许的工具(可选)</Label>
              <Input
                id="ttools"
                value={form.tools}
                onChange={(e) => setForm({ ...form, tools: e.target.value })}
                placeholder="留空允许全部;如 tavily_search,tavily_extract"
                className="font-mono text-xs"
              />
              <p className="text-xs text-muted-foreground">
                逗号分隔的工具白名单,get_my_usage 始终可用;等级门禁(crawl/map/research 需完整等级)仍然生效
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button onClick={handleCreate} disabled={creating || !form.name.trim()}>
              {creating ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!created} onOpenChange={(open) => !open && setCreated(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Token 创建成功</DialogTitle>
            <DialogDescription>
              请立即复制保存。出于安全考虑,明文不会再显示,服务器只保存哈希。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <CopyRow
              label="访问 Token"
              hint="用于 Authorization: Bearer 请求头"
              value={created?.token ?? ''}
              copied={copied === 'token'}
              onCopy={async () => {
                await navigator.clipboard.writeText(created?.token ?? '')
                setCopied('token')
                toast.success('已复制 Token')
              }}
            />
            <CopyRow
              label="MCP 地址(推荐,直接填入客户端)"
              hint="Token 附在 URL 上,无需配置请求头"
              value={created ? `${window.location.origin}/mcp?token=${created.token}` : ''}
              copied={copied === 'url'}
              onCopy={async () => {
                await navigator.clipboard.writeText(
                  created ? `${window.location.origin}/mcp?token=${created.token}` : '',
                )
                setCopied('url')
                toast.success('已复制 MCP 地址')
              }}
            />
            <CopyRow
              label="Claude Code 命令"
              value={
                created
                  ? `claude mcp add --transport http tavily-pool ${window.location.origin}/mcp?token=${created.token}`
                  : ''
              }
              copied={copied === 'cli'}
              onCopy={async () => {
                await navigator.clipboard.writeText(
                  created
                    ? `claude mcp add --transport http tavily-pool ${window.location.origin}/mcp?token=${created.token}`
                    : '',
                )
                setCopied('cli')
                toast.success('已复制命令')
              }}
            />
          </div>
          <DialogFooter>
            <Button onClick={() => setCreated(null)}>我已保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
