import type { Config } from 'tailwindcss'

// Tailwind 配置：仅扫描 src，避免误收集
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        up: '#d23f31',
        down: '#0a9b3e',
      },
    },
  },
  plugins: [],
} satisfies Config
