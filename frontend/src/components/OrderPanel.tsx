import {
  Alert,
  Button,
  ButtonGroup,
  Card,
  CardContent,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import { useState } from 'react'
import type { ReactElement } from 'react'

import type { AccountDTO, JudgeVerdict, Side } from '../api/types'
import { fmtMoney, fmtPrice } from '../utils/format'

interface Props {
  account: AccountDTO | null
  judgeVerdict?: JudgeVerdict | null
  disabled?: boolean
  busy?: boolean
  onSubmit: (side: Side, shares: number) => void
}

const LOT = 100 // A 股一手 100 股

/** 下单面板：股数 + 快捷仓位 + **一键全仓二次确认**（N15）。 */
export default function OrderPanel({ account, judgeVerdict, disabled, busy, onSubmit }: Props): ReactElement {
  const [side, setSide] = useState<Side>('buy')
  const [shares, setShares] = useState<number>(LOT)
  const [confirmFull, setConfirmFull] = useState(false)

  const price = account?.position.last_price || account?.price || 0
  const cash = account?.cash ?? 0
  const available = account?.position.available_shares ?? 0

  const maxBuy = price > 0 ? Math.max(0, Math.floor((cash * 0.999) / price / LOT) * LOT) : 0
  const maxSell = Math.floor(available / LOT) * LOT
  const cap = side === 'buy' ? maxBuy : maxSell

  const clamp = (n: number) => {
    const lot = Math.max(0, Math.floor(n / LOT) * LOT)
    return Math.min(lot, cap)
  }

  const setQuick = (fraction: number, isFull = false) => {
    if (isFull) {
      setConfirmFull(true)
      return
    }
    setShares(clamp(Math.floor(cap * fraction)))
  }

  const doFull = () => {
    setShares(cap)
    setConfirmFull(false)
  }

  const canSubmit = !disabled && !busy && shares > 0 && shares <= cap

  return (
    <Card>
      <CardContent>
        <Typography variant="overline" color="text.secondary">
          下单
        </Typography>
        <Stack spacing={1.5} sx={{ mt: 1 }}>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={side}
            onChange={(_, v: Side | null) => {
              if (v) {
                setSide(v)
                setShares(LOT)
              }
            }}
          >
            <ToggleButton value="buy" sx={{ px: 3 }}>
              买入
            </ToggleButton>
            <ToggleButton value="sell" sx={{ px: 3 }}>
              卖出
            </ToggleButton>
          </ToggleButtonGroup>

          <Stack direction="row" spacing={1} alignItems="center">
            <TextField
              label="股数"
              type="number"
              size="small"
              value={shares}
              onChange={(e) => setShares(parseInt(e.target.value || '0', 10))}
              inputProps={{ step: LOT, min: 0 }}
              sx={{ width: 140 }}
            />
            <Typography variant="body2" color="text.secondary" className="mono">
              ≈ {fmtMoney(shares * price)} 元 @ {fmtPrice(price)}
            </Typography>
          </Stack>

          <ButtonGroup size="small" variant="outlined">
            <Button onClick={() => setQuick(0.25)}>1/4</Button>
            <Button onClick={() => setQuick(1 / 3)}>1/3</Button>
            <Button onClick={() => setQuick(0.5)}>半仓</Button>
            <Button color="warning" onClick={() => setQuick(1, true)}>
              一键全仓
            </Button>
          </ButtonGroup>

          <Typography variant="caption" color="text.secondary">
            可买 {maxBuy} 股 / 可卖 {maxSell} 股（可用部分，T+1）
          </Typography>

          {judgeVerdict && (
            <Alert severity={judgeVerdict === 'agree' ? 'success' : 'info'} sx={{ py: 0 }}>
              模型建议评判：{judgeVerdict === 'agree' ? '采纳' : '未采纳'}（仅记录，不判对错）
            </Alert>
          )}

          <Button variant="contained" disabled={!canSubmit} onClick={() => onSubmit(side, shares)}>
            {busy ? '提交中…' : `${side === 'buy' ? '买入' : '卖出'} ${shares} 股`}
          </Button>
        </Stack>
      </CardContent>

      <Dialog open={confirmFull} onClose={() => setConfirmFull(false)}>
        <DialogTitle>确认全仓？</DialogTitle>
        <DialogContent>
          <DialogContentText>
            一键全仓（{cap} 股）是不可逆的大动作，请再次确认。
            <br />
            方向：{side === 'buy' ? '买入' : '卖出'}，预计金额 ≈ {fmtMoney(cap * price)} 元。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmFull(false)}>取消</Button>
          <Button color="warning" variant="contained" onClick={doFull}>
            确认全仓
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  )
}
