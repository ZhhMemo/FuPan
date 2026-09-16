import { create } from 'zustand'

import type { AccountDTO, Advice, JudgeVerdict, KlineResponse, Question, Settlement } from '../api/types'

interface QuestionState {
  question: Question | null
  kline: KlineResponse | null
  account: AccountDTO | null
  advice: Advice | null
  settlement: Settlement | null
  judgeVerdict: JudgeVerdict | null
  lastOrderId: string | null
  loading: boolean
  error: string | null
  setQuestion: (q: Question | null) => void
  setKline: (k: KlineResponse | null) => void
  setAccount: (a: AccountDTO | null) => void
  setAdvice: (a: Advice | null) => void
  setSettlement: (s: Settlement | null) => void
  setJudgeVerdict: (v: JudgeVerdict | null) => void
  setLastOrderId: (id: string | null) => void
  setLoading: (v: boolean) => void
  setError: (e: string | null) => void
  reset: () => void
}

const empty = {
  question: null,
  kline: null,
  account: null,
  advice: null,
  settlement: null,
  judgeVerdict: null,
  lastOrderId: null,
  loading: false,
  error: null,
}

/** 当前题目 / 行情 / 账户 / 建议 / 结算 的会话态。 */
export const useQuestionStore = create<QuestionState>((set) => ({
  ...empty,
  setQuestion: (question) => set({ question }),
  setKline: (kline) => set({ kline }),
  setAccount: (account) => set({ account }),
  setAdvice: (advice) => set({ advice }),
  setSettlement: (settlement) => set({ settlement }),
  setJudgeVerdict: (judgeVerdict) => set({ judgeVerdict }),
  setLastOrderId: (lastOrderId) => set({ lastOrderId }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  reset: () => set({ ...empty }),
}))
