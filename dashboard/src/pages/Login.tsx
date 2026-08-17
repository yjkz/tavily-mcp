import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Lock, Megaphone } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { api, type PublicInfo } from '@/api'

export default function Login() {
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
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

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await api.login(password)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-4">
        <Card>
          <CardHeader className="text-center">
            <img
              src="/site-icon"
              alt="logo"
              className="mx-auto mb-2 size-14 rounded-xl object-cover ring-1 ring-foreground/10"
            />
            <CardTitle className="text-xl">{info?.site_name ?? 'Tavily Pool'} 控制台</CardTitle>
            <CardDescription className="flex items-center justify-center gap-1">
              <Lock className="size-3" />
              请输入管理员密码登录
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={submit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="password">密码</Label>
                <Input
                  id="password"
                  type="password"
                  autoFocus
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <Button type="submit" className="w-full" disabled={loading || !password}>
                {loading ? '登录中…' : '登录'}
              </Button>
            </form>
          </CardContent>
        </Card>
        {info?.announcement && (
          <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
            <Megaphone className="mt-0.5 size-4 shrink-0 text-amber-500" />
            <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-amber-200/90">
              {info.announcement}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
