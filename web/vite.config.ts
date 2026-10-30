/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/research": "http://127.0.0.1:8000",
      "/runs": "http://127.0.0.1:8000",
      "/sessions": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/index": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      // Atlas 任务 API（统一工作台 /atlas 页）：/atlas-api/tasks/... → 127.0.0.1:8001/tasks/...
      "/atlas-api": {
        target: process.env.ATLAS_API_URL || "http://127.0.0.1:8001",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/atlas-api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
