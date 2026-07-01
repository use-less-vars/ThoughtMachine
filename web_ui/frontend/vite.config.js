import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: parseInt(process.env.VITE_PORT) || 5173,
    // Proxy API/WS requests to the FastAPI backend during development
    proxy: {
      '/ws': {
        target: `ws://localhost:${parseInt(process.env.VITE_BACKEND_PORT) || 8000}`,
        ws: true,
      },
      '/api': {
        target: `http://localhost:${parseInt(process.env.VITE_BACKEND_PORT) || 8000}`,
        changeOrigin: true,
      },
    },
  },
})
