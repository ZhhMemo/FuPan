import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'
import type { ReactElement } from 'react'

import type { Bar, KlineResponse } from '../api/types'

interface Props {
  bars: Bar[]
  indicators?: KlineResponse['indicators']
  height?: number
  title?: string
}

const MA_COLORS = ['#f2a900', '#3f8cff', '#b060ff', '#00a0a0']

/**
 * K 线图：**只用传入的 bars**（后端已严格截断于决策点，红线①）。
 * 均线来自后端 indicators，本组件不自行计算、不请求额外数据。
 */
export default function KLineChart({ bars, indicators, height = 420, title }: Props): ReactElement {
  const option = useMemo(() => {
    const dates = bars.map((b) => b.date)
    const candle = bars.map((b) => [b.open, b.close, b.low, b.high])
    const maSeries = indicators?.ma
      ? Object.entries(indicators.ma.series).map(([name, values], idx) => ({
          name: name.toUpperCase(),
          type: 'line',
          data: values,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.2, color: MA_COLORS[idx % MA_COLORS.length] },
          itemStyle: { color: MA_COLORS[idx % MA_COLORS.length] },
        }))
      : []

    return {
      animation: false,
      title: title ? { text: title, left: 8, textStyle: { fontSize: 13, fontWeight: 600 } } : undefined,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { top: 4, right: 8, data: maSeries.map((s) => s.name) },
      grid: { left: 56, right: 16, top: title ? 44 : 28, bottom: 44 },
      xAxis: { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#999' } } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: '#eee' } } },
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', height: 18, bottom: 8 },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: candle,
          itemStyle: {
            color: '#d23f31',
            color0: '#0a9b3e',
            borderColor: '#d23f31',
            borderColor0: '#0a9b3e',
          },
        },
        ...maSeries,
      ],
    }
  }, [bars, indicators, height, title])

  if (!bars.length) {
    return <div className="text-sm text-gray-500 py-8 text-center">暂无可显示的行情数据</div>
  }
  return <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />
}
