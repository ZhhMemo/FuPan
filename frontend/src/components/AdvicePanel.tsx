import { Card, CardContent, Chip, Divider, Stack, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material'
import type { ReactElement } from 'react'

import type { Advice, JudgeVerdict } from '../api/types'
import { fmtPct } from '../utils/format'

interface Props {
  advice: Advice | null
  verdict: JudgeVerdict | null
  onVerdict: (v: JudgeVerdict | null) => void
}

const TIER_LABEL: Record<Advice['tier'], string> = { high: '积极', mid: '中性', low: '谨慎' }
const TIER_COLOR: Record<Advice['tier'], 'error' | 'warning' | 'success'> = {
  high: 'error',
  mid: 'warning',
  low: 'success',
}

/** 判卷模式：模型建议 → 用户评判（采纳/未采纳，不判对错）。 */
export default function AdvicePanel({ advice, verdict, onVerdict }: Props): ReactElement {
  return (
    <Card>
      <CardContent>
        <Typography variant="overline" color="text.secondary">
          判卷模式 · 模型建议（N6）
        </Typography>
        {!advice ? (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            暂无建议
          </Typography>
        ) : (
          <Stack spacing={1} sx={{ mt: 1 }}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Chip size="small" color={TIER_COLOR[advice.tier]} label={TIER_LABEL[advice.tier]} />
              <Typography variant="body2" className="mono">
                目标仓位 {fmtPct(advice.target_position, 0)}
              </Typography>
            </Stack>
            <Typography variant="body2">{advice.reason_text}</Typography>
            <Typography variant="caption" color="text.secondary">
              MA20={advice.ma ?? '—'} / 收盘={advice.close ?? '—'} / 规则={advice.rule_version}
            </Typography>
            <Divider sx={{ my: 0.5 }} />
            <Typography variant="body2" color="text.secondary">
              你的评判（仅用于模式分账，不做对错判定）：
            </Typography>
            <ToggleButtonGroup
              size="small"
              exclusive
              value={verdict}
              onChange={(_, v: JudgeVerdict | null) => onVerdict(v)}
            >
              <ToggleButton value="agree">采纳</ToggleButton>
              <ToggleButton value="disagree">未采纳</ToggleButton>
            </ToggleButtonGroup>
          </Stack>
        )}
      </CardContent>
    </Card>
  )
}
