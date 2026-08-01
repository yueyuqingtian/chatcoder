import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // 桌面版用 file:// 加载,必须用相对路径,否则资源找不到导致黑屏
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8010",
      "/ws": {
        target: "ws://localhost:8010",
        ws: true,
      },
    },
  },
  resolve: {
    alias: {
      "@": "/src",
      "@chatcoder/shared": "/../packages/shared/src/index.ts",
    },
  },
});
