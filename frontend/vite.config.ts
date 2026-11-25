import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8001";

const proxy = {
  target: backend,
  changeOrigin: true,
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": proxy,
      "/auth": proxy,
      "/repos": proxy,
      "/sandboxes": proxy,
      "/tasks": proxy,
    },
  },
});
