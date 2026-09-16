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

/** 下单面板：股数 + 快捷仓位（空仓/1⁄4/1⁄3/半仓/全仓/清仓）+ **一键全仓/清仓二次确认**（N15 / FR-3.4）。 */
export default function OrderPanel({ account, judgeVerdict, disabled, busy, onSubmit }: Props): ReactElement {
  const [side, setSide] = useState<Side>('buy')
  const [shares, setShares] = useState<number>(LOT)
  const [confirmMode, setConfirmMode] = useState<null | 'full' | 'liquidate'>(null)

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

  const setQuick = (fraction: number) => {
    setShares(clamp(Math.floor(cap * fraction)))
  }

  // 空仓：不持有（买入方向归零，等同观望）
  const setEmpty = () => {
    setSide('buy')
    setShares(0)
  }

  // 一键全仓（买入可用上限，需二次确认）
  const requestFull = () => setConfirmMode('full')
  // 清仓：卖出全部可用（需二次确认）
  const requestLiquidate = () => setConfirmMode('liquidate')

  const confirmAction = () => {
    if (confirmMode === 'full') {
      setSide('buy')
      setShares(maxBuy)
    } else if (confirmMode === 'liquidate') {
      setSide('sell')
      setShares(maxSell)
    }
    setConfirmMode(null)
  }

  const canSubmit = !disabled && !busy && shares > 0 && shares <= cap
  const confirmQty = confirmMode === 'liquidate' ? maxSell : maxBuy
  const confirmSide: Side = confirmMode === 'liquidate' ? 'sell' : 'buy'

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

          <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
            <ButtonGroup size="small" variant="outlined">
              <Button onClick={setEmpty}>空仓</Button>
              <Button onClick={() => setQuick(0.25)}>1/4</Button>
              <Button onClick={() => setQuick(1 / 3)}>1/3</Button>
              <Button onClick={() => setQuick(0.5)}>半仓</Button>
            </ButtonGroup>
            <ButtonGroup size="small" variant="outlined">
              <Button color="warning" onClick={requestFull} disabled={maxBuy <= 0}>
                一键全仓
              </Button>
              <Button color="error" onClick={requestLiquidate} disabled={maxSell <= 0}>
                清仓
              </Button>
            </ButtonGroup>
          </Stack>

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

      <Dialog open={confirmMode !== null} onClose={() => setConfirmMode(null)}>
        <DialogTitle>{confirmMode === 'liquidate' ? '确认清仓？' : '确认全仓？'}</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {confirmMode === 'liquidate'
              ? `清仓将卖出全部可用持仓（${confirmQty} 股），是不可逆的大动作，请再次确认。`
              : `一键全仓（${confirmQty} 股）是不可逆的大动作，请再次确认。`}
            <br />
            方向：{confirmSide === 'buy' ? '买入' : '卖出'}，预计金额 ≈ {fmtMoney(confirmQty * price)} 元。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmMode(null)}>取消</Button>
          <Button
            color={confirmMode === 'liquidate' ? 'error' : 'warning'}
            variant="contained"
            onClick={confirmAction}
          >
            {confirmMode === 'liquidate' ? '确认清仓' : '确认全仓'}
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  )
}
