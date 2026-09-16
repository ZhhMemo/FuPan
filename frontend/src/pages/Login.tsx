import { Alert, Box, Button, Card, CardContent, Stack, TextField, Typography } from '@mui/material'
import { useState } from 'react'
import type { FormEvent, ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { useAuthStore } from '../store/auth'

export default function Login(): ReactElement {
  const login = useAuthStore((s) => s.login)
  const error = useAuthStore((s) => s.error)
  const loggingIn = useAuthStore((s) => s.loggingIn)
  const navigate = useNavigate()
  const location = useLocation() as { state?: { from?: string } }
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    try {
      await login(username, password)
      navigate(location.state?.from ?? '/', { replace: true })
    } catch {
      /* 错误已写入 store */
    }
  }

  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', pt: 8 }}>
      <Card sx={{ width: 380 }}>
        <CardContent>
          <Typography variant="h5" gutterBottom fontWeight={600}>
            登录
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            单账号 · 密码慢哈希 · 登录失败限速
          </Typography>
          <form onSubmit={onSubmit}>
            <Stack spacing={2}>
              <TextField
                label="用户名"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                fullWidth
                autoComplete="username"
              />
              <TextField
                label="密码"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                fullWidth
                autoComplete="current-password"
              />
              {error && <Alert severity="error">{error}</Alert>}
              <Button type="submit" variant="contained" disabled={loggingIn} fullWidth>
                {loggingIn ? '登录中…' : '登录'}
              </Button>
            </Stack>
          </form>
        </CardContent>
      </Card>
    </Box>
  )
}
