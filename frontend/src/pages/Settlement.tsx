import { Alert, Box, Button, Card, CardContent, Divider, Stack, Typography } from '@mui/material'
import { useEffect, useState } from 'react'
import type { ReactElement } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import type { Settlement as SettlementDTO } from '../api/types'
import { fmtPct, fmtPrice } from '../utils/format'
import { useQuestionStore } from '../store/question'

export default function Settlement(): ReactElement {
  const navigate = useNavigate()
  const question = useQuestionStore((s) => s.question)
  const [data, setData] = useState<SettlementDTO | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = async (qid: string) => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.settle(qid))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (question) void load(question.question_id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question?.question_id])

  const rows: [string, string][] = data
    ? [
        ['账户收益率', fmtPct(data.account_return)],
        ['标的区间收益', fmtPct(data.stock_return)],
        ['基准（沪深300）', fmtPct(data.benchmark_return)],
        ['超额收益', fmtPct(data.alpha)],
        ['机会成本', fmtPct(data.opp_cost)],
        ['最大回撤', fmtPct(data.max_dd)],
        ['全程持有对照', fmtPct(data.hold_all_return)],
        ['成交价', fmtPrice(data.entry_price)],
        ['退出价', fmtPrice(data.exit_price)],
        ['退出日', data.exit_day ?? '—'],
        ['持有交易日', `${data.hold_days}`],
        ['退市平仓', data.delisted ? '是（最后可交易日）' : '否'],
      ]
    : []

  return (
    <Stack spacing={2}>
      <Card>
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            结算（账户视角 · 不判对错）
          </Typography>
          {!question && (
            <Alert severity="info" sx={{ mt: 1 }}>
              请先返回首页手工创建一道题并完成下单。
              <Button size="small" sx={{ ml: 2 }} onClick={() => navigate('/')}>
                去出题
              </Button>
            </Alert>
          )}
          {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
          {loading && <Typography variant="body2" sx={{ mt: 1 }}>加载中…</Typography>}
        </CardContent>
      </Card>

      {data && (
        <Card>
          <CardContent>
            <Typography variant="subtitle1" fontWeight={600} gutterBottom>
              {question?.title || question?.code} · 账户结果
            </Typography>
            <Stack spacing={0.75}>
              {rows.map(([k, v], i) => (
                <div key={k}>
                  {i === 3 && <Divider sx={{ my: 0.5 }} />}
                  <Stack direction="row" justifyContent="space-between">
                    <Typography variant="body2" color="text.secondary">
                      {k}
                    </Typography>
                    <Typography variant="body2" className="mono">
                      {v}
                    </Typography>
                  </Stack>
                </div>
              ))}
            </Stack>
            <Box sx={{ mt: 2 }}>
              <Typography variant="caption" color="text.secondary">
                {data.notes} · 费率版本 {data.fee_version} · 结算仅呈现收益/风险指标，无对错判定。
              </Typography>
            </Box>
          </CardContent>
        </Card>
      )}
    </Stack>
  )
}
