import { useEffect, useRef, useState } from 'react'
import { Bell, ImagePlus, KeyRound, Loader2, Megaphone, RefreshCcw, Save } from 'lucide-react'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { api, type SiteSettings } from '@/api'

type AlertForm = {
  channel: string
  url: string
  secret: string
  smtpHost: string
  smtpPort: string
  smtpSsl: boolean
  emailUser: string
  emailPass: string
  emailFrom: string
  emailTo: string
  keyDisabled: boolean
  keyExhausted: boolean
  poolExhausted: boolean
  minActive: string
  minRemaining: string
}

const EMPTY_ALERT: AlertForm = {
  channel: '',
  url: '',
  secret: '',
  smtpHost: '',
  smtpPort: '465',
  smtpSsl: true,
  emailUser: '',
  emailPass: '',
  emailFrom: '',
  emailTo: '',
  keyDisabled: true,
  keyExhausted: false,
  poolExhausted: true,
  minActive: '0',
  minRemaining: '0',
}

const WEBHOOK_CHANNELS = ['feishu', 'wecom', 'dingtalk', 'generic']

export default function SettingsPage() {
  const [settings, setSettings] = useState<SiteSettings | null>(null)
  const [siteName, setSiteName] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [savingSite, setSavingSite] = useState(false)
  const [iconVersion, setIconVersion] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [pw, setPw] = useState({ current: '', next: '', confirm: '' })
  const [savingPw, setSavingPw] = useState(false)
  const [alert, setAlert] = useState<AlertForm>(EMPTY_ALERT)
  const [savingAlert, setSavingAlert] = useState(false)
  const [testingAlert, setTestingAlert] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = () => {
    api
      .getSettings()
      .then((s) => {
        setSettings(s)
        setSiteName(s.site_name)
        setAnnouncement(s.announcement)
        setIconVersion((v) => v + 1)
        setAlert({
          channel: s.alert.channel || '',
          url: s.alert.webhook_url || '',
          secret: s.alert.webhook_secret || '',
          smtpHost: s.alert.email_smtp_host || '',
          smtpPort: String(s.alert.email_smtp_port ?? 465),
          smtpSsl: s.alert.email_smtp_ssl !== false,
          emailUser: s.alert.email_username || '',
          emailPass: s.alert.email_password || '',
          emailFrom: s.alert.email_from || '',
          emailTo: s.alert.email_to || '',
          keyDisabled: s.alert.on_key_disabled,
          keyExhausted: s.alert.on_key_exhausted,
          poolExhausted: s.alert.on_pool_exhausted,
          minActive: String(s.alert.pool_min_active ?? 0),
          minRemaining: String(s.alert.pool_min_remaining ?? 0),
        })
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

  const saveAlert = async () => {
    if (WEBHOOK_CHANNELS.includes(alert.channel) && !alert.url.trim()) {
      toast.error('已选择 Webhook 渠道,请填写 Webhook 地址')
      return
    }
    if (alert.channel === 'email' && (!alert.smtpHost.trim() || !alert.emailUser.trim() || !alert.emailPass || !alert.emailTo.trim())) {
      toast.error('邮件告警需要填写 SMTP 服务器、发信邮箱、密码/授权码和收件邮箱')
      return
    }
    setSavingAlert(true)
    try {
      await api.updateSettings({
        alert_channel: alert.channel,
        alert_webhook_url: alert.url.trim(),
        alert_webhook_secret: alert.secret.trim(),
        alert_email_smtp_host: alert.smtpHost.trim(),
        alert_email_smtp_port: Number(alert.smtpPort) || 465,
        alert_email_smtp_ssl: alert.smtpSsl,
        alert_email_username: alert.emailUser.trim(),
        alert_email_password: alert.emailPass,
        alert_email_from: alert.emailFrom.trim(),
        alert_email_to: alert.emailTo.trim(),
        alert_on_key_disabled: alert.keyDisabled,
        alert_on_key_exhausted: alert.keyExhausted,
        alert_on_pool_exhausted: alert.poolExhausted,
        alert_pool_min_active: Number(alert.minActive) || 0,
        alert_pool_min_remaining: Number(alert.minRemaining) || 0,
      })
      toast.success('告警配置已保存')
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSavingAlert(false)
    }
  }

  const alertTestReady =
    WEBHOOK_CHANNELS.includes(alert.channel)
      ? !!alert.url.trim()
      : alert.channel === 'email' &&
        !!alert.smtpHost.trim() &&
        !!alert.emailUser.trim() &&
        !!alert.emailPass &&
        !!alert.emailTo.trim()

  const testAlert = async () => {
    setTestingAlert(true)
    try {
      const r = await api.alertTest()
      if (r.ok) toast.success('测试消息已发送,请到对应群/客户端查看')
      else toast.error(r.error ?? '发送失败')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '发送失败')
    } finally {
      setTestingAlert(false)
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

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Bell className="size-4" />
            告警通知
          </CardTitle>
          <CardDescription>
            Key 池健康事件推送到飞书 / 企业微信 / 钉钉 / 通用 Webhook / 邮件(SMTP)。不选渠道即为关闭。
            同一事件按冷却时间去重,不会刷屏。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="space-y-2">
              <Label>告警渠道</Label>
              <Select value={alert.channel || 'off'} onValueChange={(v) => setAlert({ ...alert, channel: v === 'off' ? '' : v })}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="off">关闭(不告警)</SelectItem>
                  <SelectItem value="email">邮件(SMTP)</SelectItem>
                  <SelectItem value="feishu">飞书机器人</SelectItem>
                  <SelectItem value="wecom">企业微信机器人</SelectItem>
                  <SelectItem value="dingtalk">钉钉机器人</SelectItem>
                  <SelectItem value="generic">通用 Webhook(JSON)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {WEBHOOK_CHANNELS.includes(alert.channel) && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="alert-url">Webhook 地址</Label>
                  <Input
                    id="alert-url"
                    value={alert.url}
                    onChange={(e) => setAlert({ ...alert, url: e.target.value })}
                    placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="alert-secret">签名密钥(可选)</Label>
                  <Input
                    id="alert-secret"
                    value={alert.secret}
                    onChange={(e) => setAlert({ ...alert, secret: e.target.value })}
                    placeholder="开启签名的机器人密钥"
                  />
                </div>
              </>
            )}
          </div>

          {alert.channel === 'email' && (
            <div className="grid gap-4 lg:grid-cols-3">
              <div className="space-y-2">
                <Label htmlFor="alert-smtp-host">SMTP 服务器</Label>
                <Input
                  id="alert-smtp-host"
                  value={alert.smtpHost}
                  onChange={(e) => setAlert({ ...alert, smtpHost: e.target.value })}
                  placeholder="如 smtp.163.com"
                />
                <p className="text-xs text-muted-foreground">
                  只发信不需要 IMAP。常见:163/QQ 用 465(SSL),Gmail 用 smtp.gmail.com:587
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="alert-smtp-port">SMTP 端口</Label>
                <Input
                  id="alert-smtp-port"
                  type="number"
                  value={alert.smtpPort}
                  onChange={(e) => setAlert({ ...alert, smtpPort: e.target.value })}
                  placeholder="465"
                />
              </div>
              <div className="flex items-center justify-between rounded-lg border px-4 py-3">
                <div className="pr-4">
                  <div className="text-sm font-medium">SSL 加密</div>
                  <div className="text-xs text-muted-foreground">465 端口开启;587 走 STARTTLS 时关闭</div>
                </div>
                <Switch checked={alert.smtpSsl} onCheckedChange={(v) => setAlert({ ...alert, smtpSsl: v })} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="alert-email-user">发信邮箱(登录账号)</Label>
                <Input
                  id="alert-email-user"
                  value={alert.emailUser}
                  onChange={(e) => setAlert({ ...alert, emailUser: e.target.value })}
                  placeholder="sender@example.com"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="alert-email-pass">密码 / 授权码</Label>
                <Input
                  id="alert-email-pass"
                  type="password"
                  value={alert.emailPass}
                  onChange={(e) => setAlert({ ...alert, emailPass: e.target.value })}
                  placeholder="163/QQ 邮箱请填授权码而非登录密码"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="alert-email-from">发件人(可选)</Label>
                <Input
                  id="alert-email-from"
                  value={alert.emailFrom}
                  onChange={(e) => setAlert({ ...alert, emailFrom: e.target.value })}
                  placeholder="留空用发信邮箱;可填显示名,如:Tavily Pool 网关"
                />
                <p className="text-xs text-muted-foreground">
                  填显示名时自动以发信邮箱作为实际地址;也可直接填完整格式:名字 &lt;邮箱&gt;
                </p>
              </div>
              <div className="space-y-2 lg:col-span-3">
                <Label htmlFor="alert-email-to">收件邮箱</Label>
                <Input
                  id="alert-email-to"
                  value={alert.emailTo}
                  onChange={(e) => setAlert({ ...alert, emailTo: e.target.value })}
                  placeholder="接收告警的邮箱,多个用英文逗号分隔"
                />
              </div>
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-3">
            <div className="flex items-center justify-between rounded-lg border px-4 py-3">
              <div className="pr-4">
                <div className="text-sm font-medium">单个 key 被禁用</div>
                <div className="text-xs text-muted-foreground">401 失效时通知(30 分钟去重)</div>
              </div>
              <Switch checked={alert.keyDisabled} onCheckedChange={(v) => setAlert({ ...alert, keyDisabled: v })} />
            </div>
            <div className="flex items-center justify-between rounded-lg border px-4 py-3">
              <div className="pr-4">
                <div className="text-sm font-medium">单个 key 配额耗尽</div>
                <div className="text-xs text-muted-foreground">免费池较吵,默认关闭</div>
              </div>
              <Switch checked={alert.keyExhausted} onCheckedChange={(v) => setAlert({ ...alert, keyExhausted: v })} />
            </div>
            <div className="flex items-center justify-between rounded-lg border px-4 py-3">
              <div className="pr-4">
                <div className="text-sm font-medium">全池不可用</div>
                <div className="text-xs text-muted-foreground">可用 key 数为 0 时通知(10 分钟去重)</div>
              </div>
              <Switch checked={alert.poolExhausted} onCheckedChange={(v) => setAlert({ ...alert, poolExhausted: v })} />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="alert-min-active">可用 key 数告警阈值(0 = 关)</Label>
              <Input
                id="alert-min-active"
                type="number"
                min={0}
                value={alert.minActive}
                onChange={(e) => setAlert({ ...alert, minActive: e.target.value })}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="alert-min-remaining">剩余 credits 告警阈值(0 = 关)</Label>
              <Input
                id="alert-min-remaining"
                type="number"
                min={0}
                value={alert.minRemaining}
                onChange={(e) => setAlert({ ...alert, minRemaining: e.target.value })}
              />
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button onClick={saveAlert} disabled={savingAlert}>
              {savingAlert ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存告警配置
            </Button>
            <Button variant="outline" onClick={testAlert} disabled={testingAlert || !alertTestReady}>
              {testingAlert ? <Loader2 className="size-4 animate-spin" /> : <Bell className="size-4" />}
              发送测试告警
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
