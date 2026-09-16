import { Card, CardContent, Divider, Stack, Typography } from '@mui/material'
import type { ReactElement } from 'react'

import type { AccountDTO } from '../api/types'
import { fmtMoney, fmtPct } from '../utils/format'

/** 持仓与资金面板（FR-3.3）。 */
export default function PositionPanel({ account }: { account: AccountDTO | null }): ReactElement {
  if (!account) {
    return (
      <Card>
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            账户
          </Typography>
          <Typography variant="body2" color="text.secondary">
            暂无数据
          </Typography>
        </CardContent>
      </Card>
    )
  }
  const pos = account.position
  const rows: [string, string][] = [
    ['现金', fmtMoney(account.cash)],
    ['总资产', fmtMoney(account.total_asset)],
    ['仓位', fmtPct(account.position_ratio)],
    ['持仓股数', `${pos.shares}`],
    ['可用股数', `${pos.available_shares}${pos.available_shares < pos.shares ? '（T+1）' : ''}`],
    ['成本价', pos.shares > 0 ? fmtMoney(pos.avg_cost) : '—'],
    ['最新价', fmtMoney(pos.last_price)],
    ['市值', fmtMoney(pos.market_value)],
    ['浮动盈亏', pos.shares > 0 ? fmtMoney(pos.unrealized_pnl) : '—'],
  ]
  return (
    <Card>
      <CardContent>
        <Typography variant="overline" color="text.secondary">
          账户（{account.question_id ?? ''}）
        </Typography>
        <Stack spacing={0.5} sx={{ mt: 1 }}>
          {rows.map(([k, v], i) => (
            <div key={k}>
              {i === 2 && <Divider sx={{ my: 0.75 }} />}
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
      </CardContent>
    </Card>
  )
}
