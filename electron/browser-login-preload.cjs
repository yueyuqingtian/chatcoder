"use strict";

// plan-771-3177：只监听顶层网页登录表单，IPC 私有于沙箱内的隔离世界。
const { ipcRenderer } = require("electron");

// 沙箱 preload 不保证暴露 process.isMainFrame（Electron 沙箱子集有限），缺省时用 window.top 判定主框架
const isMainFrame = typeof process.isMainFrame === "boolean" ? process.isMainFrame : window.top === window;

if (isMainFrame && /^(https?:)$/.test(location.protocol)) {
  let protectedDocument = false;
  const isLoginPassword = (field) => field instanceof HTMLInputElement
    && field.type === "password" && field.autocomplete !== "new-password" && !field.disabled;
  const protect = (event) => {
    if (protectedDocument || !event.isTrusted || !isLoginPassword(event.target)) return;
    protectedDocument = true;
    ipcRenderer.send("browserVault:protectLogin", location.origin);
  };
  window.addEventListener("focusin", protect, true);
  window.addEventListener("input", protect, true);
  window.addEventListener("submit", (event) => {
    const form = event.target;
    if (!event.isTrusted || !(form instanceof HTMLFormElement)) return;
    const fields = Array.from(form.elements);
    const password = fields.find((field) => isLoginPassword(field) && field.value);
    if (!password) return;
    const accounts = fields.slice(0, fields.indexOf(password)).filter((field) =>
      field instanceof HTMLInputElement && !field.disabled && ["text", "email", "tel"].includes(field.type) && field.value.trim());
    const username = (accounts.find((field) => field.autocomplete === "username") || accounts.at(-1))?.value.trim();
    if (username) ipcRenderer.send("browserVault:offerLogin", {
      origin: location.origin, username: username.slice(0, 300), password: password.value.slice(0, 4096),
    });
  }, true);
}
