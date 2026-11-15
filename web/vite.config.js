import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173; /api goes to the local token server on :8000.
export default defineConfig({
  plugins: [react()],
  build: {
    // separate cacheable chunks for livekit-client and React
    rollupOptions: { output: { manualChunks: { livekit: ["livekit-client"], react: ["react", "react-dom"] } } },
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
