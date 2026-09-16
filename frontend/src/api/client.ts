import axios, { AxiosError } from 'axios'

import type {
  AccountDTO,
  Advice,
  CustomQuestionRequest,
  Envelope,
  KlineResponse,
  LoginResponse,
  OrderRequest,
  OrderResponse,
  Question,
  Settlement,
} from './types'

const TOKEN_KEY = 'fupan_token'
const USER_KEY = 'fupan_user'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null, username?: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
    if (username) localStorage.setItem(USER_KEY, username)
  } else {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }
}

export function getUsername(): string | null {
  return localStorage.getItem(USER_KEY)
}

export const http = axios.create({ baseURL: '/api', timeout: 20000 })

// 请求拦截：附加 Bearer 令牌
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers ?? {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：解包 {code,data,message}；401 清理登录态
http.interceptors.response.use(
  (resp) => resp,
  (error: AxiosError<Envelope<unknown>>) => {
    if (error.response?.status === 401) {
      setToken(null)
    }
    const message = error.response?.data?.message ?? error.message ?? '请求失败'
    return Promise.reject(new Error(message))
  },
)

async function unwrap<T>(p: Promise<{ data: Envelope<T> }>): Promise<T> {
  const { data } = await p
  if (data.code !== 0) {
    throw new Error(data.message || '请求失败')
  }
  return data.data
}

export const api = {
  // 认证
  login: (username: string, password: string) =>
    unwrap<LoginResponse>(http.post('/auth/login', { username, password })),
  logout: () => unwrap<Record<string, never>>(http.post('/auth/logout')),
  me: () => unwrap<{ username: string }>(http.get('/auth/me')),

  // 出题与观察
  createQuestion: (payload: CustomQuestionRequest) =>
    unwrap<Question>(http.post('/questions/custom', payload)),
  getQuestion: (qid: string) => unwrap<Question>(http.get(`/questions/${qid}`)),
  getKline: (qid: string, adjust: string = 'qfq', indicators: string = 'ma') =>
    unwrap<KlineResponse>(http.get(`/questions/${qid}/kline`, { params: { adjust, indicators } })),
  getAdvice: (qid: string) => unwrap<Advice>(http.get(`/questions/${qid}/advice`)),

  // 下单 / 账户
  placeOrder: (payload: OrderRequest) => unwrap<OrderResponse>(http.post('/trade/order', payload)),
  getAccount: (qid: string) => unwrap<AccountDTO>(http.get(`/trade/account/${qid}`)),

  // 结算
  settle: (qid: string, window?: number) => unwrap<Settlement>(http.post(`/settle/${qid}`, { window })),
}
