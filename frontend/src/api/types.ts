// 后端 DTO 的 TS 镜像类型（字段名与后端逐字对齐，见 backend/app/api/schemas.py 与 models.py）

export interface Envelope<T> {
  code: number
  data: T
  message: string
  detail?: unknown
}

export interface LoginResponse {
  token: string
  expires_at: string
  username: string
}

export type PositionType = 'empty' | 'holding'
export type QuestionMode = 'normal' | 'judge'
export type Side = 'buy' | 'sell'
export type JudgeVerdict = 'agree' | 'disagree'
export type AdjustMode = 'qfq' | 'hfq' | 'none'

export interface InitialPosition {
  shares: number
  avg_cost: number
  cost_date?: string | null
}

export interface Question {
  question_id: string
  code: string
  start_date: string
  end_date: string
  pattern_tag: string
  position_type: PositionType
  position_state_tier: string | null
  initial_position: InitialPosition | null
  initial_cash: number
  source: string
  visible_until: string
  settle_window: number | null
  settle_params: Record<string, unknown>
  decision_points: string[]
  question_mode: QuestionMode
  title: string
  description: string
  key_points: string
  difficulty_prior: number | null
  difficulty_post: number | null
  created_at: string
}

export interface CustomQuestionRequest {
  code: string
  start_date: string
  end_date: string
  position_type: PositionType
  position_state_tier?: string | null
  initial_position?: InitialPosition | null
  initial_cash: number
  settle_window?: number | null
  question_mode: QuestionMode
}

export interface Bar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface KlineResponse {
  code: string
  adjust: AdjustMode
  bars: Bar[]
  indicators: {
    ma?: {
      dates: string[]
      series: Record<string, (number | null)[]>
    }
  }
  visible_until: string
  cutoff: string
  requested_indicators: string[]
}

export interface Advice {
  tier: 'high' | 'mid' | 'low'
  target_position: number
  reason_text: string
  rule_version: string
  ma: number | null
  close: number | null
  slope: number | null
}

export interface FeeDetail {
  side: Side
  shares: number
  ref_price: number
  exec_price: number
  turnover: number
  commission: number
  stamp_tax: number
  transfer_fee: number
  slippage: number
  total_fee: number
  fee_version: string
}

export interface Fill {
  accepted: boolean
  code: string
  side: Side
  shares: number
  price: number
  ref_price: number
  dt: string
  cash_after: number
  position_after: number
  realized_pnl: number
  reason: string
  deferred_to: string | null
  decision_point: string | null
  fee: FeeDetail | null
}

export interface PositionDTO {
  code: string
  shares: number
  available_shares: number
  avg_cost: number
  last_price: number
  market_value: number
  unrealized_pnl: number
}

export interface AccountDTO {
  question_id?: string
  price?: number
  cash: number
  initial_cash: number
  position: PositionDTO
  total_asset: number
  position_ratio: number
}

export interface OrderRequest {
  question_id: string
  side: Side
  shares: number
  judge_verdict?: JudgeVerdict | null
}

export interface OrderResponse {
  order_id: string
  est_cost: number | null
  fill: Fill
  account: AccountDTO
}

export interface Settlement {
  order_id: string
  account_return: number
  stock_return: number
  benchmark_return: number
  alpha: number
  opp_cost: number
  max_dd: number
  hold_all_return: number
  entry_price: number
  exit_price: number
  exit_day: string | null
  hold_days: number
  delisted: boolean
  fee_version: string
  notes: string
}
