import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: parseInt(process.env.VITE_PORT) || 5173,
    // Proxy API/WS requests to the FastAPI backend during development.
    // Use 127.0.0.1 (not localhost): Node >= 17 resolves localhost to ::1
    // (IPv6) first, but uvicorn binds 127.0.0.1 (IPv4) only, so a localhost
    // target fails with ECONNREFUSED ::1:8000 on Debian-family systems.
    proxy: {
      '/ws': {
        target: `ws://127.0.0.1:${parseInt(process.env.VITE_BACKEND_PORT) || 8000}`,
        ws: true,
      },
      '/api': {
        target: `http://127.0.0.1:${parseInt(process.env.VITE_BACKEND_PORT) || 8000}`,
        changeOrigin: true,
      },
    },
  },
})
