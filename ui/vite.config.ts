import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` on :5173 proxies the admin API to the Gateway on :8000.
// Prod: `npm run build`; the Gateway serves ui/dist itself.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true, changeOrigin: true } },
  },
});
