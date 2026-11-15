import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the page runs on :5173 and proxies API and WebSocket calls to FastAPI on :8000.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/demo": { target: "http://localhost:8000", ws: true, changeOrigin: false },
      "/healthz": "http://localhost:8000",
    },
  },
});
