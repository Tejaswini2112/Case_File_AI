import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server runs on :5173 while FastAPI runs on :8000. A browser will not
// let a page served from one origin call another, so every API path is proxied
// through this server instead — the page only ever talks to its own origin and
// the cross-origin question never arises.
//
// Proxying rather than adding CORS middleware to FastAPI keeps the Python side
// unchanged, and matches production: there the built files are served by
// FastAPI itself, so front end and API share an origin for real. Adding CORS
// would mean loosening the API permanently to solve a problem that only exists
// in development.
const API_PATHS = ['/ask', '/health', '/openapi.json']

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: Object.fromEntries(
      API_PATHS.map((path) => [path, { target: 'http://127.0.0.1:8000', changeOrigin: true }]),
    ),
  },
})
