/** 宠物窗口入口（plan-73-323 阶段2）。
 *
 * 独立 Vite 入口：产出独立 chunk，不增加主窗包体积。
 * 主题跟随系统：宠物页拿不到主窗的主题偏好，而窗口本身透明、浮层自带不透明语义底色，
 * 跟随 `prefers-color-scheme` 即可与桌面对齐，不会出现"深色桌面上出现白板"。
 */
import { createRoot } from "react-dom/client";
import { PetApp } from "./PetApp";
import "./pet.css";

function syncTheme() {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}

syncTheme();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncTheme);

const el = document.getElementById("pet-root");
if (el) {
  createRoot(el).render(<PetApp />);
}
