import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The Flask API runs on :5001; proxying /api avoids hard-coding its URL in the app.
// `npm run build` writes to the repo-root public/ folder, which Vercel serves from its CDN
// next to the Flask function (see vercel.json).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3010,
    proxy: { '/api': process.env.API_URL || 'http://localhost:5001' },
  },
  build: {
    outDir: '../public',
    emptyOutDir: true,
  },
})
