/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: UI on :5173 proxies /api to the FastAPI backend on :8000. Prod: `npm run build` -> dist/, served by the backend itself.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], css: false },
});
