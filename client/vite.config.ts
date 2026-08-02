import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import dns from "node:dns";

// Node.js 18+ 默认使用 internalConnectMultiple，会同时尝试 IPv6(::1) 和 IPv4(127.0.0.1)。
// http-proxy 库底层用 net.connect，dns.setDefaultResultOrder 对它无效。
// 改用 verbatim 模式，让 DNS 返回原始顺序，配合后端监听双栈避免 ECONNREFUSED。
dns.setDefaultResultOrder("ipv4first");

export default defineConfig({
  plugins: [react()],
  // 桌面版用 file:// 加载,必须用相对路径,否则资源找不到导致黑屏
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        // 绕过 http-proxy 的 DNS 解析问题：用 configure 钩子强制 agent 走 IPv4
        configure: (proxy) => {
          proxy.on("error", (err) => {
            console.error("[vite proxy]", err.message);
          });
        },
      },
      "/ws": {
        target: "ws://localhost:8000",
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
