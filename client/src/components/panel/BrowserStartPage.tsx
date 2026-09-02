/**
 * 浏览器起始页（空状态组件）
 * 当新建标签页且尚未输入网址时显示
 */
import { useState } from "react";
import { IconGlobe, IconArrowRight, IconTarget, IconTerminal, IconCode } from "../icons";

const POPULAR_SITES = [
  { name: "本地开发", url: "http://localhost:5173", icon: "⚡" },
  { name: "本地 3000", url: "http://localhost:3000", icon: "💻" },
  { name: "本地 8080", url: "http://localhost:8080", icon: "🌐" },
  { name: "GitHub", url: "https://github.com", icon: "🐙" },
  { name: "MDN Web Docs", url: "https://developer.mozilla.org", icon: "📚" },
  { name: "Tailwind CSS", url: "https://tailwindcss.com", icon: "🎨" },
];

export function BrowserStartPage({ onNavigate }: { onNavigate: (url: string) => void }) {
  const [inputUrl, setInputUrl] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputUrl.trim()) {
      onNavigate(inputUrl.trim());
    }
  };

  return (
    <div className="browser-start-page">
      <div className="browser-start-card">
        <div className="browser-start-logo">
          <IconGlobe size={32} />
        </div>
        <h2 className="browser-start-title">内置浏览器</h2>
        <p className="browser-start-subtitle">支持多标签浏览、实时元素标注、DevTools 与 AI 协同镜像</p>

        <form onSubmit={handleSubmit} className="browser-start-form">
          <input
            type="text"
            className="browser-start-input"
            placeholder="输入网址（如 localhost:5173 或 https://example.com）"
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            spellCheck={false}
            autoCapitalize="none"
            autoCorrect="off"
          />
          <button type="submit" className="browser-start-btn" title="访问">
            <IconArrowRight size={14} />
          </button>
        </form>

        <div className="browser-start-quick">
          <div className="browser-start-quick-title">快捷入口</div>
          <div className="browser-start-quick-grid">
            {POPULAR_SITES.map((site) => (
              <button
                key={site.url}
                className="browser-start-quick-item"
                onClick={() => onNavigate(site.url)}
              >
                <span className="browser-start-quick-icon">{site.icon}</span>
                <span className="browser-start-quick-name">{site.name}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="browser-start-features">
          <div className="browser-start-feature">
            <IconTarget size={14} />
            <span>鼠标悬停自动聚焦并标注元素属性</span>
          </div>
          <div className="browser-start-feature">
            <IconCode size={14} />
            <span>一键捕获 DOM 结构与截图导入聊天</span>
          </div>
          <div className="browser-start-feature">
            <IconTerminal size={14} />
            <span>AI 可直接操作并向你提供可视化反馈</span>
          </div>
        </div>
      </div>
    </div>
  );
}
