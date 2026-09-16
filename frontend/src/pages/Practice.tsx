import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  MenuItem,
  Snackbar,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useState } from 'react'
import type { ChangeEvent, ReactElement } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import type { CustomQuestionRequest, PositionType, QuestionMode, Side } from '../api/types'
import AdvicePanel from '../components/AdvicePanel'
import KLineChart from '../components/KLineChart'
import OrderPanel from '../components/OrderPanel'
import PositionPanel from '../components/PositionPanel'
import { useQuestionStore } from '../store/question'

const todayISO = () => new Date().toISOString().slice(0, 10)

export default function Practice(): ReactElement {
  const navigate = useNavigate()
  const {
    question,
    kline,
    account,
    advice,
    judgeVerdict,
    setQuestion,
    setKline,
    setAccount,
    setAdvice,
    setJudgeVerdict,
    setLoading,
    loading,
    error,
    setError,
  } = useQuestionStore()

  const [form, setForm] = useState<CustomQuestionRequest>({
    code: 'sz.300750',
    start_date: '2020-08-01',
    end_date: '2020-08-20',
    position_type: 'empty',
    initial_cash: 100000,
    question_mode: 'normal',
  })
  const [posShares, setPosShares] = useState(1000)
  const [posCost, setPosCost] = useState(180)
  const [toast, setToast] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refreshAccount = async (qid: string) => {
    try {
      setAccount(await api.getAccount(qid))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const loadQuestion = async (qid: string) => {
    setLoading(true)
    try {
      const q = await api.getQuestion(qid)
      setQuestion(q)
      const k = await api.getKline(qid, 'qfq', 'ma')
      setKline(k)
      await refreshAccount(qid)
      if (q.question_mode === 'judge') {
        try {
          setAdvice(await api.getAdvice(qid))
        } catch {
          setAdvice(null)
        }
      } else {
        setAdvice(null)
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const onCreate = async () => {
    setLoading(true)
    setError(null)
    try {
      const payload: CustomQuestionRequest = {
        ...form,
        initial_position:
          form.position_type === 'holding' ? { shares: posShares, avg_cost: posCost } : null,
      }
      const q = await api.createQuestion(payload)
      setJudgeVerdict(null)
      await loadQuestion(q.question_id)
      setToast('题目已创建')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const onOrder = async (side: Side, shares: number) => {
    if (!question) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.placeOrder({
        question_id: question.question_id,
        side,
        shares,
        judge_verdict: question.question_mode === 'judge' ? judgeVerdict : null,
      })
      setAccount(res.account)
      setToast(`已成交：${side === 'buy' ? '买入' : '卖出'} ${shares} 股 @ ${res.fill.price}`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onSettle = async () => {
    if (!question) return
    setLoading(true)
    setError(null)
    try {
      await api.settle(question.question_id)
      navigate('/settlement')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const upd = (key: keyof CustomQuestionRequest) => (e: ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }))

  return (
    <Stack spacing={2}>
      <Card>
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            手工出题（M1）
          </Typography>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 2, mt: 1 }}>
            <TextField label="代码" size="small" value={form.code} onChange={upd('code')} />
            <TextField
              label="开始日期"
              type="date"
              size="small"
              value={form.start_date}
              onChange={upd('start_date')}
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              label="决策点 end_date"
              type="date"
              size="small"
              value={form.end_date}
              onChange={upd('end_date')}
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              label="初始资金"
              type="number"
              size="small"
              value={form.initial_cash}
              onChange={(e) => setForm((f) => ({ ...f, initial_cash: Number(e.target.value) }))}
            />
            <TextField
              select
              label="持仓类型"
              size="small"
              value={form.position_type}
              onChange={(e) => setForm((f) => ({ ...f, position_type: e.target.value as PositionType }))}
            >
              <MenuItem value="empty">空仓型</MenuItem>
              <MenuItem value="holding">持仓型</MenuItem>
            </TextField>
            <TextField
              select
              label="题目模式"
              size="small"
              value={form.question_mode}
              onChange={(e) => setForm((f) => ({ ...f, question_mode: e.target.value as QuestionMode }))}
            >
              <MenuItem value="normal">普通判断</MenuItem>
              <MenuItem value="judge">判卷模式</MenuItem>
            </TextField>
            {form.position_type === 'holding' && (
              <>
                <TextField
                  label="初始股数"
                  type="number"
                  size="small"
                  value={posShares}
                  onChange={(e) => setPosShares(Number(e.target.value))}
                />
                <TextField
                  label="初始成本价"
                  type="number"
                  size="small"
                  value={posCost}
                  onChange={(e) => setPosCost(Number(e.target.value))}
                />
              </>
            )}
          </Box>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <Button variant="contained" onClick={onCreate} disabled={loading}>
              创建题目
            </Button>
            {question && (
              <Button variant="outlined" onClick={onSettle} disabled={loading}>
                结算（下单后）
              </Button>
            )}
          </Stack>
        </CardContent>
      </Card>

      {error && <Alert severity="error">{error}</Alert>}

      {question && (
        <Card>
          <CardContent>
            <Stack direction="row" spacing={2} alignItems="baseline">
              <Typography variant="subtitle1" fontWeight={600}>
                {question.title || question.code}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {question.code} · {question.start_date} → 决策点 {question.end_date} ·{' '}
                {question.question_mode === 'judge' ? '判卷模式' : '普通判断'}
              </Typography>
            </Stack>
          </CardContent>
        </Card>
      )}

      <Box sx={{ display: 'grid', gridTemplateColumns: '2.2fr 1fr', gap: 2 }}>
        <Box>
          <Card>
            <CardContent>
              <Typography variant="overline" color="text.secondary">
                K 线（前复权，严格截断于决策点 {question?.end_date ?? '-'}）
              </Typography>
              <KLineChart bars={kline?.bars ?? []} indicators={kline?.indicators} />
            </CardContent>
          </Card>
        </Box>
        <Stack spacing={2}>
          {question?.question_mode === 'judge' && (
            <AdvicePanel advice={advice} verdict={judgeVerdict} onVerdict={setJudgeVerdict} />
          )}
          <PositionPanel account={account} />
          <OrderPanel
            account={account}
            judgeVerdict={judgeVerdict}
            disabled={!question}
            busy={busy}
            onSubmit={onOrder}
          />
        </Stack>
      </Box>

      <Snackbar
        open={!!toast}
        autoHideDuration={2500}
        onClose={() => setToast(null)}
        message={toast ?? ''}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      />
    </Stack>
  )
}
