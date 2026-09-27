import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
// Development proxies /api to the backend. Defaults to the local uvicorn
// server; set VITE_PROXY_TARGET in .env.local to point at a deployment.
export default defineConfig(({ mode }) => { const env = loadEnv(mode, process.cwd(), ''); return { plugins: [react()], server: { proxy: { '/api': { target: env.VITE_PROXY_TARGET || 'http://localhost:8000', changeOrigin: true, rewrite: path => path.replace(/^\/api/, '') } } } }; });
