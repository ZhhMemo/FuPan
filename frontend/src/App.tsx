import { AppBar, Box, Button, Container, Toolbar, Typography } from '@mui/material'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import type { ReactElement } from 'react'

import Login from './pages/Login'
import Practice from './pages/Practice'
import Settlement from './pages/Settlement'
import { useAuthStore } from './store/auth'

function RequireAuth({ children }: { children: ReactElement }): ReactElement {
  const token = useAuthStore((s) => s.token)
  const location = useLocation()
  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return children
}

function TopBar(): ReactElement {
  const username = useAuthStore((s) => s.username)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  return (
    <AppBar position="static" color="primary" elevation={0}>
      <Toolbar variant="dense">
        <Typography variant="h6" sx={{ flexGrow: 1, fontWeight: 600 }}>
          复盘 · A 股历史决策训练
        </Typography>
        {username && (
          <Typography variant="body2" sx={{ mr: 2 }}>
            {username}
          </Typography>
        )}
        <Button
          color="inherit"
          size="small"
          onClick={async () => {
            await logout()
            navigate('/login', { replace: true })
          }}
        >
          登出
        </Button>
      </Toolbar>
    </AppBar>
  )
}

export default function App(): ReactElement {
  const token = useAuthStore((s) => s.token)
  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {token && <TopBar />}
      <Container maxWidth="lg" sx={{ py: 3, flexGrow: 1 }}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Practice />
              </RequireAuth>
            }
          />
          <Route
            path="/settlement"
            element={
              <RequireAuth>
                <Settlement />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Container>
    </Box>
  )
}
