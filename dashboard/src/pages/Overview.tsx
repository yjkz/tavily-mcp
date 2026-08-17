import { useEffect, useState } from 'react'
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Activity, Coins, KeyRound, ShieldCheck } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { api, type DailyStat, type Overview } from '@/api'

function StatCard({
  icon: Icon,
  title,
  value,
  sub,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  value: React.ReactNode
  sub?: React.ReactNode
}) {
  return (
    <Card>
      <CardContent className="flex items-start justify-between">
        <div>
          <div className="text-sm text-muted-foreground">{title}</div>
          <div className="mt-1.5 text-2xl font-semibold tabular-nums">{value}</div>
          {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
        </div>
        <div className="flex size-9 items-center justify-center rounded-lg bg-muted">
          <Icon className="size-4.5 text-muted-foreground" />
        </div>
      </CardContent>
    </Card>
  )
}

const STATUS_META: Record<string, { label: string; dot: string }> = {
  active: { label: '正常', dot: 'bg-emerald-500' },
  cooling: { label: '冷却中', dot: 'bg-amber-500' },
  exhausted: { label: '已耗尽', dot: 'bg-orange-500' },
  disabled: { label: '已禁用', dot: 'bg-zinc-500' },
}

export default function OverviewPage() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [daily, setDaily] = useState<DailyStat[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.overview(), api.dailyStats(14)])
      .then(([o, d]) => {
        setOverview(o)
        setDaily(d)
      })
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
  }, [])

  if (error) return <p className="text-sm text-destructive">{error}</p>
  if (!overview || !daily) {
    return (
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
        <Skeleton className="h-72" />
      </div>
    )
  }

  const poolPercent =
    overview.pool_capacity_limit > 0
      ? Math.min(100, (overview.pool_credits_used / overview.pool_capacity_limit) * 100)
      : 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">概览</h1>
        <p className="mt-1 text-sm text-muted-foreground">网关运行状态与用量总览</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={Activity}
          title="今日请求"
          value={overview.today_requests}
          sub={`成功 ${overview.today_success} 次`}
        />
        <StatCard
          icon={Coins}
          title="本月 Credits"
          value={overview.month_credits.toLocaleString()}
          sub={`共 ${overview.month_requests} 次成功调用`}
        />
        <StatCard
          icon={KeyRound}
          title="Key 池健康"
          value={
            <>
              {overview.keys_status.active}
              <span className="text-base font-normal text-muted-foreground">
                {' '}
                / {overview.keys_total}
              </span>
            </>
          }
          sub={
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              {Object.entries(overview.keys_status)
                .filter(([, count]) => count > 0)
                .map(([status, count]) => (
                  <span key={status} className="inline-flex items-center gap-1">
                    <span className={`size-1.5 rounded-full ${STATUS_META[status].dot}`} />
                    {STATUS_META[status].label} {count}
                  </span>
                ))}
            </span>
          }
        />
        <StatCard
          icon={ShieldCheck}
          title="活跃 Token"
          value={overview.tokens_active}
          sub={`历史总调用 ${overview.total_key_requests.toLocaleString()} 次`}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">近 14 天趋势</CardTitle>
          <CardDescription>每日请求数 / 错误数(柱)与 Credits 消耗(线)</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={daily}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.08)" vertical={false} />
                <XAxis dataKey="date" stroke="rgba(255,255,255,0.45)" fontSize={12} tickLine={false} />
                <YAxis stroke="rgba(255,255,255,0.45)" fontSize={12} tickLine={false} axisLine={false} />
                <ChartTooltip
                  contentStyle={{
                    background: '#1c1c1f',
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: 8,
                    fontSize: 12,
                    color: '#eee',
                  }}
                />
                <Bar dataKey="requests" name="请求" fill="#6366f1" radius={[3, 3, 0, 0]} />
                <Bar dataKey="errors" name="错误" fill="#ef4444" radius={[3, 3, 0, 0]} />
                <Line
                  dataKey="credits"
                  name="Credits"
                  stroke="#10b981"
                  strokeWidth={2}
                  dot={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Key 池本月配额</CardTitle>
          <CardDescription>
            全部 key 合计已用 {overview.pool_credits_used.toLocaleString()} /{' '}
            {overview.pool_capacity_limit.toLocaleString()} credits(以 Tavily 实际用量为准)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Progress value={poolPercent} className="h-3" />
          <div className="mt-2 text-right text-xs text-muted-foreground">
            {poolPercent.toFixed(1)}%
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
