import { useCallback, useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, RefreshCcw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api, formatTs, type LogsPage, type TokenItem } from '@/api'

const PAGE_SIZE = 50

const STATUS_STYLE: Record<string, string> = {
  success: 'bg-emerald-500/15 text-emerald-500 border-emerald-500/30',
  upstream_error: 'bg-red-500/15 text-red-400 border-red-500/30',
  pool_exhausted: 'bg-orange-500/15 text-orange-500 border-orange-500/30',
  rate_limited: 'bg-amber-500/15 text-amber-500 border-amber-500/30',
  quota_exceeded: 'bg-amber-500/15 text-amber-500 border-amber-500/30',
  tier_denied: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
}

const STATUS_LABEL: Record<string, string> = {
  success: '成功',
  upstream_error: '上游错误',
  pool_exhausted: '池耗尽',
  rate_limited: '限流',
  quota_exceeded: '配额超限',
  tier_denied: '等级不足',
}

const TOOLS = ['tavily_search', 'tavily_extract', 'tavily_crawl', 'tavily_map', 'tavily_research', 'get_my_usage']

export default function LogsPage() {
  const [data, setData] = useState<LogsPage | null>(null)
  const [tokens, setTokens] = useState<TokenItem[]>([])
  const [tokenId, setTokenId] = useState('')
  const [status, setStatus] = useState('')
  const [tool, setTool] = useState('')
  const [page, setPage] = useState(0)

  const load = useCallback(() => {
    setData(null)
    api
      .logs({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        token_id: tokenId || undefined,
        status: status || undefined,
        tool: tool || undefined,
      })
      .then(setData)
      .catch(() => setData({ total: 0, items: [] }))
  }, [page, tokenId, status, tool])

  useEffect(load, [load])
  useEffect(() => {
    api.tokens().then(setTokens).catch(() => {})
  }, [])

  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">请求日志</h1>
          <p className="mt-1 text-sm text-muted-foreground">最近 30 天的全部 MCP 调用记录</p>
        </div>
        <Button variant="outline" onClick={load}>
          <RefreshCcw className="size-4" />
          刷新
        </Button>
      </div>

      <div className="flex flex-wrap gap-3">
        <Select
          value={tokenId || 'all'}
          onValueChange={(v) => {
            setPage(0)
            setTokenId(v === 'all' ? '' : v)
          }}
        >
          <SelectTrigger className="w-44">
            <SelectValue placeholder="Token" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部 Token</SelectItem>
            {tokens.map((t) => (
              <SelectItem key={t.id} value={String(t.id)}>
                {t.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={status || 'all'}
          onValueChange={(v) => {
            setPage(0)
            setStatus(v === 'all' ? '' : v)
          }}
        >
          <SelectTrigger className="w-36">
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            {Object.keys(STATUS_LABEL).map((s) => (
              <SelectItem key={s} value={s}>
                {STATUS_LABEL[s]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={tool || 'all'}
          onValueChange={(v) => {
            setPage(0)
            setTool(v === 'all' ? '' : v)
          }}
        >
          <SelectTrigger className="w-44">
            <SelectValue placeholder="工具" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部工具</SelectItem>
            {TOOLS.map((t) => (
              <SelectItem key={t} value={t}>
                {t}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {!data ? (
        <Skeleton className="h-96" />
      ) : (
        <>
          <div className="rounded-xl border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>Token</TableHead>
                  <TableHead>工具</TableHead>
                  <TableHead>查询内容</TableHead>
                  <TableHead>上游 Key</TableHead>
                  <TableHead>来源 IP</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">HTTP</TableHead>
                  <TableHead className="text-right">Credits</TableHead>
                  <TableHead className="text-right">延迟</TableHead>
                  <TableHead>详情</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={11} className="py-10 text-center text-muted-foreground">
                      没有匹配的日志
                    </TableCell>
                  </TableRow>
                )}
                {data.items.map((log) => (
                  <TableRow key={log.id}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatTs(log.ts)}
                    </TableCell>
                    <TableCell className="max-w-32 truncate">{log.token_name}</TableCell>
                    <TableCell className="font-mono text-xs">{log.tool}</TableCell>
                    <TableCell
                      className="max-w-56 truncate text-xs text-muted-foreground"
                      title={log.query ?? ''}
                    >
                      {log.query ?? '-'}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{log.tavily_key}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {log.client_ip}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className={STATUS_STYLE[log.status] ?? ''}>
                        {STATUS_LABEL[log.status] ?? log.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {log.http_status ?? '-'}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {log.credits ? log.credits : '-'}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {log.latency_ms != null ? `${log.latency_ms}ms` : '-'}
                    </TableCell>
                    <TableCell
                      className="max-w-48 truncate text-xs text-muted-foreground"
                      title={log.error_detail ?? ''}
                    >
                      {log.error_detail ?? (log.request_id ? `req: ${log.request_id}` : '-')}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>共 {data.total.toLocaleString()} 条</span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
              >
                <ChevronLeft className="size-4" />
                上一页
              </Button>
              <span className="tabular-nums">
                {page + 1} / {totalPages}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={page + 1 >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
