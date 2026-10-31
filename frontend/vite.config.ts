import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev: /api/* is proxied to the FastAPI backend, mirroring nginx.conf.template in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.BACKEND_URL ?? 'http://localhost:8000',
        changeOrigin: true,
        rewrite: path => path.replace(/^\/api/, ''),
      },
    },
  },
});
