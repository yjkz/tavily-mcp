import { useEffect, useRef, useState } from 'react'
import { ImagePlus, KeyRound, Loader2, Megaphone, RefreshCcw, Save } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { api, type SiteSettings } from '@/api'

export default function SettingsPage() {
  const [settings, setSettings] = useState<SiteSettings | null>(null)
  const [siteName, setSiteName] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [savingSite, setSavingSite] = useState(false)
  const [iconVersion, setIconVersion] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [pw, setPw] = useState({ current: '', next: '', confirm: '' })
  const [savingPw, setSavingPw] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = () => {
    api
      .getSettings()
      .then((s) => {
        setSettings(s)
        setSiteName(s.site_name)
        setAnnouncement(s.announcement)
        setIconVersion((v) => v + 1)
      })
      .catch((e) => toast.error(e instanceof Error ? e.message : '加载失败'))
  }
  useEffect(reload, [])

  const saveSite = async () => {
    setSavingSite(true)
    try {
      await api.updateSettings({ site_name: siteName, announcement })
      toast.success('已保存:站名与公告即时生效(登录页/横幅/MCP 用量接口可见)')
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSavingSite(false)
    }
  }

  const uploadIcon = async (file: File | undefined) => {
    if (!file) return
    setUploading(true)
    try {
      await api.uploadIcon(file)
      toast.success('图标已更新(浏览器缓存约 5 分钟后全面生效)')
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '上传失败')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const resetIcon = async () => {
    await api.deleteIcon().catch((e) => toast.error(e instanceof Error ? e.message : '操作失败'))
    toast.success('已恢复默认图标')
    reload()
  }

  const savePassword = async () => {
    if (pw.next.length < 8) {
      toast.error('新密码至少 8 位')
      return
    }
    if (pw.next !== pw.confirm) {
      toast.error('两次输入的新密码不一致')
      return
    }
    setSavingPw(true)
    try {
      await api.changePassword(pw.current, pw.next)
      toast.success('密码已修改,下次登录使用新密码')
      setPw({ current: '', next: '', confirm: '' })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '修改失败')
    } finally {
      setSavingPw(false)
    }
  }

  if (!settings) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-48" />
        <Skeleton className="h-48" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">网站设置</h1>
        <p className="mt-1 text-sm text-muted-foreground">站点信息、公告、图标与管理员密码</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Megaphone className="size-4" />
              站名与公告
            </CardTitle>
            <CardDescription>
              公告会显示在控制台顶部横幅、登录页,以及 MCP 客户端调用 get_my_usage
              时的返回中;清空即关闭公告。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="sitename">网站名称</Label>
              <Input
                id="sitename"
                value={siteName}
                maxLength={40}
                onChange={(e) => setSiteName(e.target.value)}
                placeholder="Tavily Pool"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="announcement">公告内容</Label>
              <Textarea
                id="announcement"
                rows={4}
                maxLength={2000}
                value={announcement}
                onChange={(e) => setAnnouncement(e.target.value)}
                placeholder="例如:今晚 22:00-23:00 维护,期间搜索可能不可用"
              />
              <p className="text-xs text-muted-foreground">
                {announcement.length}/2000 字,留空则不显示公告
              </p>
            </div>
            <Button onClick={saveSite} disabled={savingSite}>
              {savingSite ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存
            </Button>
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <ImagePlus className="size-4" />
                网站图标
              </CardTitle>
              <CardDescription>
                支持 PNG / JPEG / SVG / WebP,不超过 1MB。显示在浏览器标签页、登录页与侧边栏。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex items-center gap-4">
              <img
                src={`/site-icon?v=${iconVersion}`}
                alt="当前图标"
                className="size-16 rounded-xl object-cover ring-1 ring-foreground/10"
              />
              <div className="flex flex-col gap-2">
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/png,image/jpeg,image/svg+xml,image/webp"
                  className="hidden"
                  onChange={(e) => uploadIcon(e.target.files?.[0])}
                />
                <Button variant="outline" disabled={uploading} onClick={() => fileRef.current?.click()}>
                  {uploading ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    <ImagePlus className="size-4" />
                  )}
                  {uploading ? '上传中…' : '上传新图标'}
                </Button>
                {settings.has_custom_icon && (
                  <Button variant="ghost" className="text-muted-foreground" onClick={resetIcon}>
                    <RefreshCcw className="size-4" />
                    恢复默认
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <KeyRound className="size-4" />
                修改管理员密码
              </CardTitle>
              <CardDescription>
                修改后立即生效(保存在数据库,优先于环境变量中的初始密码)。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="pw-current">当前密码</Label>
                <Input
                  id="pw-current"
                  type="password"
                  value={pw.current}
                  onChange={(e) => setPw({ ...pw, current: e.target.value })}
                />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="pw-next">新密码(≥8 位)</Label>
                  <Input
                    id="pw-next"
                    type="password"
                    value={pw.next}
                    onChange={(e) => setPw({ ...pw, next: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="pw-confirm">确认新密码</Label>
                  <Input
                    id="pw-confirm"
                    type="password"
                    value={pw.confirm}
                    onChange={(e) => setPw({ ...pw, confirm: e.target.value })}
                  />
                </div>
              </div>
              <Button onClick={savePassword} disabled={savingPw || !pw.current || !pw.next}>
                {savingPw ? <Loader2 className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                修改密码
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
