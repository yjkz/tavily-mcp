import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  KeyRound,
  ScrollText,
  Settings2,
  ShieldCheck,
  LogOut,
  Megaphone,
  X,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, type PublicInfo } from '@/api'
import { toast } from 'sonner'

const nav = [
  { to: '/', label: '概览', icon: LayoutDashboard, end: true },
  { to: '/keys', label: 'Key 池', icon: KeyRound, end: false },
  { to: '/tokens', label: '访问 Token', icon: ShieldCheck, end: false },
  { to: '/logs', label: '请求日志', icon: ScrollText, end: false },
  { to: '/settings', label: '网站设置', icon: Settings2, end: false },
]

const DISMISS_KEY = 'tpm_announcement_dismissed'

function AnnouncementBanner({ info }: { info: PublicInfo }) {
  const [dismissed, setDismissed] = useState(
    () => localStorage.getItem(DISMISS_KEY) === String(info.announcement_updated_at),
  )
  if (!info.announcement || dismissed) return null
  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, String(info.announcement_updated_at))
    setDismissed(true)
  }
  return (
    <div className="mb-6 flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
      <Megaphone className="mt-0.5 size-4 shrink-0 text-amber-500" />
      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-amber-200/90">
        {info.announcement}
      </p>
      <button
        onClick={dismiss}
        className="shrink-0 rounded-md p-1 text-amber-500/70 hover:bg-amber-500/10"
        title="关闭(公告更新后会再次显示)"
      >
        <X className="size-4" />
      </button>
    </div>
  )
}

export default function Layout() {
  const navigate = useNavigate()
  const [info, setInfo] = useState<PublicInfo | null>(null)

  useEffect(() => {
    api
      .publicInfo()
      .then((i) => {
        setInfo(i)
        if (i.site_name) document.title = `${i.site_name} 控制台`
      })
      .catch(() => {})
  }, [])

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r bg-sidebar">
        <div className="flex items-center gap-2 px-5 py-5">
          <img src="/site-icon" alt="logo" className="size-8 rounded-lg object-cover" />
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold leading-tight">
              {info?.site_name ?? 'Tavily Pool'}
            </div>
            <div className="text-xs text-muted-foreground">MCP 网关控制台</div>
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-1 px-3">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                    : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground'
                }`
              }
            >
              <item.icon className="size-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="p-3">
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start text-muted-foreground"
            onClick={async () => {
              await api.logout().catch(() => {})
              toast.success('已退出登录')
              navigate('/login')
            }}
          >
            <LogOut className="size-4" />
            退出登录
          </Button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-background">
        <div className="mx-auto max-w-6xl px-6 py-8">
          {info && <AnnouncementBanner info={info} />}
          <Outlet />
        </div>
      </main>
    </div>
  )
}
