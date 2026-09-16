import { create } from 'zustand'

import { api, getToken, getUsername, setToken } from '../api/client'

interface AuthState {
  token: string | null
  username: string | null
  loggingIn: boolean
  error: string | null
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  clearError: () => void
}

/** 登录态（localStorage 持久化令牌）。 */
export const useAuthStore = create<AuthState>((set) => ({
  token: getToken(),
  username: getUsername(),
  loggingIn: false,
  error: null,
  login: async (username, password) => {
    set({ loggingIn: true, error: null })
    try {
      const res = await api.login(username, password)
      setToken(res.token, res.username)
      set({ token: res.token, username: res.username, loggingIn: false })
    } catch (e) {
      set({ loggingIn: false, error: (e as Error).message })
      throw e
    }
  },
  logout: async () => {
    try {
      await api.logout()
    } finally {
      setToken(null)
      set({ token: null, username: null })
    }
  },
  clearError: () => set({ error: null }),
}))
