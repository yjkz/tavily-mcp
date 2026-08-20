import { useCallback, useEffect, useState } from 'react'
import { FlaskConical, Gauge, Loader2, Plus, RefreshCcw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { api, formatTs, type KeyItem } from '@/api'

const STATUS_META: Record<string, { label: string; className: string }> = {
  active: { label: '正常', className: 'bg-emerald-500/15 text-emerald-500 border-emerald-500/30' },
  cooling: { label: '冷却中', className: 'bg-amber-500/15 text-amber-500 border-amber-500/30' },
  exhausted: { label: '已耗尽', className: 'bg-orange-500/15 text-orange-500 border-orange-500/30' },
  disabled: { label: '已禁用', className: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30' },
}

function KeyCard({
  item,
  testing,
  onTest,
  onToggle,
  onDelete,
}: {
  item: KeyItem
  testing: boolean
  onTest: (item: KeyItem) => void
  onToggle: (item: KeyItem) => void
  onDelete: (item: KeyItem) => void
}) {
  const meta = STATUS_META[item.status] ?? STATUS_META.disabled
  const percent = item.plan_limit > 0 ? Math.min(100, (item.credits_used_month / item.plan_limit) * 100) : 0
  return (
    <div className="rounded-xl border bg-card p-5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{item.label || `Key #${item.id}`}</span>
            <Badge variant="outline" className={meta.className}>
              {meta.label}
            </Badge>
          </div>
          <div className="mt-1 font-mono text-xs text-muted-foreground">{item.masked_key}</div>
        </div>
      </div>

      <div className="mt-4 space-y-1.5">
        <Progress value={percent} className="h-2" />
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>
            本月 {item.credits_used_month.toLocaleString()} / {item.plan_limit.toLocaleString()} credits
          </span>
          <span>剩余 {item.remaining_credits.toLocaleString()}</span>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>总调用:{item.total_requests.toLocaleString()} 次</span>
        <span className="text-right">最后使用:{formatTs(item.last_used_at)}</span>
      </div>
      {item.last_error && (
        <p
          className="mt-2 truncate text-xs text-orange-400"
          title={item.last_error}
        >
          {item.last_error}
        </p>
      )}

      <div className="mt-4 flex items-center gap-2">
        <Button size="sm" variant="outline" disabled={testing} onClick={() => onTest(item)}>
          {testing ? <Loader2 className="size-3.5 animate-spin" /> : <FlaskConical className="size-3.5" />}
          {testing ? '测试中…' : '测试连接'}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="text-muted-foreground"
          onClick={() => onToggle(item)}
        >
          {item.stored_status === 'disabled' ? '启用' : '禁用'}
        </Button>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="sm" variant="ghost" className="ml-auto text-destructive hover:text-destructive">
              <Trash2 className="size-3.5" />
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>删除这个 key?</AlertDialogTitle>
              <AlertDialogDescription>
                将删除 {item.label || `Key #${item.id}`}({item.masked_key})。历史日志会保留,此操作不可撤销。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>取消</AlertDialogCancel>
              <AlertDialogAction onClick={() => onDelete(item)}>删除</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  )
}

export default function KeysPage() {
  const [keys, setKeys] = useState<KeyItem[] | null>(null)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [addKeysText, setAddKeysText] = useState('')
  const [addLabel, setAddLabel] = useState('')
  const [addLimit, setAddLimit] = useState('1000')
  const [adding, setAdding] = useState(false)
  const [syncing, setSyncing] = useState(false)

  const refresh = useCallback(() => {
    api
      .keys()
      .then(setKeys)
      .catch((e) => toast.error(e instanceof Error ? e.message : '加载失败'))
  }, [])

  useEffect(refresh, [refresh])

  const handleTest = async (item: KeyItem) => {
    setTestingId(item.id)
    try {
      const result = await api.testKey(item.id)
      if (result.ok) {
        const parts = [`连接成功 · ${result.latency_ms}ms`]
        if (result.remaining != null) parts.push(`剩余 ${result.remaining.toLocaleString()} credits`)
        if (result.plan) parts.push(`计划:${result.plan}`)
        if (result.source === 'account') parts.push('按账户配额')
        if (result.recovered) parts.push('已从耗尽状态自动恢复')
        else if (result.calibrated) parts.push('配额已校准')
        toast.success(parts.join(' · '))
      } else {
        toast.error(`连接失败 · ${result.error ?? `HTTP ${result.status}`}`)
      }
      refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '测试失败')
    } finally {
      setTestingId(null)
    }
  }

  const handleToggle = async (item: KeyItem) => {
    await api.patchKey(item.id, { enabled: item.stored_status === 'disabled' }).catch((e) =>
      toast.error(e instanceof Error ? e.message : '操作失败'),
    )
    refresh()
  }

  const handleDelete = async (item: KeyItem) => {
    await api.deleteKey(item.id).catch((e) => toast.error(e instanceof Error ? e.message : '删除失败'))
    toast.success('已删除')
    refresh()
  }

  const handleAdd = async () => {
    if (!addKeysText.trim()) return
    setAdding(true)
    try {
      const result = await api.addKeys({
        keys: addKeysText,
        label: addLabel.trim(),
        plan_limit: Number(addLimit) || 1000,
      })
      toast.success(`已添加 ${result.added} 个 key` + (result.skipped_duplicates ? `,跳过重复 ${result.skipped_duplicates} 个` : ''))
      setAddOpen(false)
      setAddKeysText('')
      setAddLabel('')
      refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '添加失败')
    } finally {
      setAdding(false)
    }
  }

  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      const r = await api.syncAllKeys()
      const parts = [`成功 ${r.ok} / 失败 ${r.failed}`]
      if (r.recovered) parts.push(`恢复耗尽 key ${r.recovered} 个`)
      toast.success(`全量校准完成:${parts.join(' · ')}`)
      refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '校准失败')
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Key 池</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            管理上游 Tavily key,调度器会自动轮询、冷却与故障转移
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={refresh}>
            <RefreshCcw className="size-4" />
            刷新
          </Button>
          <Button variant="outline" disabled={syncing} onClick={handleSyncAll}>
            {syncing ? <Loader2 className="size-4 animate-spin" /> : <Gauge className="size-4" />}
            {syncing ? '校准中…' : '全量校准'}
          </Button>
          <Button onClick={() => setAddOpen(true)}>
            <Plus className="size-4" />
            批量添加
          </Button>
        </div>
      </div>

      {!keys ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-56" />
          ))}
        </div>
      ) : keys.length === 0 ? (
        <div className="rounded-xl border border-dashed p-12 text-center text-sm text-muted-foreground">
          还没有任何 key。点击右上角「批量添加」,粘贴你的 Tavily key(每行一个)。
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {keys.map((item) => (
            <KeyCard
              key={item.id}
              item={item}
              testing={testingId === item.id}
              onTest={handleTest}
              onToggle={handleToggle}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>批量添加 Tavily Key</DialogTitle>
            <DialogDescription>
              每行一个 key(以 tvly- 开头),支持一次粘贴几十个。默认按免费计划 1000 credits/月计算。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="keys">Key 列表</Label>
              <Textarea
                id="keys"
                rows={6}
                placeholder={'tvly-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\ntvly-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy'}
                value={addKeysText}
                onChange={(e) => setAddKeysText(e.target.value)}
                className="font-mono text-xs"
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="label">备注(可选)</Label>
                <Input id="label" value={addLabel} onChange={(e) => setAddLabel(e.target.value)} placeholder="如:主账号" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="limit">月度配额 (credits)</Label>
                <Input id="limit" type="number" value={addLimit} onChange={(e) => setAddLimit(e.target.value)} />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAddOpen(false)}>
              取消
            </Button>
            <Button onClick={handleAdd} disabled={adding || !addKeysText.trim()}>
              {adding ? '添加中…' : '添加'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
