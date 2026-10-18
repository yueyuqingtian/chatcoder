import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import dns from "node:dns";

// Node.js 18+ 默认使用 internalConnectMultiple，会同时尝试 IPv6(::1) 和 IPv4(127.0.0.1)。
// http-proxy 库底层用 net.connect，dns.setDefaultResultOrder 对它无效。
// 改用 verbatim 模式，让 DNS 返回原始顺序，配合后端监听双栈避免 ECONNREFUSED。
dns.setDefaultResultOrder("ipv4first");

// plan-20261017-frontend-polish：沙箱验证环境用 CHATCODER_BACKEND 指向独立后端（默认仍是本机 8000）。
const backendTarget = process.env.CHATCODER_BACKEND || "http://localhost:8000";
const backendWsTarget = backendTarget.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 桌面版用 file:// 加载,必须用相对路径,否则资源找不到导致黑屏
  base: "./",
  // plan-73-323：宠物窗口作为第二个 HTML 入口（独立置顶浮窗）——
  // 与主窗共享构建流程、产出独立 chunk，不增加主窗包体积。
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        pet: "pet.html",
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
        // 绕过 http-proxy 的 DNS 解析问题：用 configure 钩子强制 agent 走 IPv4
        configure: (proxy) => {
          proxy.on("error", (err) => {
            console.error("[vite proxy]", err.message);
          });
        },
      },
      "/ws": {
        target: backendWsTarget,
        ws: true,
      },
    },
  },
  resolve: {
    alias: [
      { find: "@", replacement: "/src" },
      { find: /^@chatcoder\/shared$/, replacement: "/../packages/shared/src/index.ts" },
      { find: /^@chatcoder\/shared\/(.+)$/, replacement: "/../packages/shared/src/$1" },
    ],
  },
});
