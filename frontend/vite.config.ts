import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Vite 配置：dev proxy → 后端 :8000（避免 CORS）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
