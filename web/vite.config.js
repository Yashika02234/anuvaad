import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the React app on :5173 and forwards /api to the local token
// server (python -m uvicorn server:app --port 8000). On Vercel, /api/token is the
// Python function in api/token.py and the built app is served from dist/.
export default defineConfig({
  plugins: [react()],
  build: {
    // livekit-client is large: keep it (and React) in their own cacheable chunks
    rollupOptions: { output: { manualChunks: { livekit: ["livekit-client"], react: ["react", "react-dom"] } } },
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
