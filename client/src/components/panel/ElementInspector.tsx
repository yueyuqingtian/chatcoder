/**
 * DevTools 风格的高性能元素检查覆盖层
 *
 * 特性：
 * 1. 鼠标悬停实时追踪元素盒模型并绘制半透明高亮框；
 * 2. 贴附式 DevTools 提示弹窗：<tag#id.class>、尺寸 (W×H)、布局、字体、文字色/背景色色块预览、无障碍/ARIA 属性；
 * 3. 自适应边界防溢出，随光标移动平滑更新；
 * 4. 支持 iframe 模式与 webview (Electron IPC 注入) 模式。
 */
import { memo } from "react";
import type { ElementInfo } from "../../store/browser";

interface ElementInspectorProps {
  hoverInfo: {
    rect: { x: number; y: number; width: number; height: number };
    info: ElementInfo;
  } | null;
  cursorPos: { x: number; y: number } | null;
  containerRect?: { width: number; height: number };
}

export const ElementInspector = memo(function ElementInspector({
  hoverInfo,
  cursorPos,
  containerRect,
}: ElementInspectorProps) {
  if (!hoverInfo) return null;

  const { rect, info } = hoverInfo;
  const cw = containerRect?.width || window.innerWidth;
  const ch = containerRect?.height || window.innerHeight;

  // 计算 Tooltip 弹窗的最佳位置（优先位于光标右下方，触底/触右翻转）
  const TIP_W = 260;
  const TIP_H = 140;
  let tipX = (cursorPos ? cursorPos.x : rect.x) + 12;
  let tipY = (cursorPos ? cursorPos.y : rect.y + rect.height) + 12;

  if (tipX + TIP_W > cw - 12) {
    tipX = Math.max(8, (cursorPos ? cursorPos.x : rect.x) - TIP_W - 12);
  }
  if (tipY + TIP_H > ch - 12) {
    tipY = Math.max(8, (cursorPos ? cursorPos.y : rect.y) - TIP_H - 12);
  }

  return (
    <>
      {/* 元素高亮覆盖框（DevTools 蓝/青色半透明填充 + 虚线边框） */}
      <div
        className="devtools-inspector-highlight"
        style={{
          position: "absolute",
          left: rect.x,
          top: rect.y,
          width: rect.width,
          height: rect.height,
          pointerEvents: "none",
          zIndex: 40,
          background: "rgba(59, 130, 246, 0.2)",
          border: "1.5px solid #2563EB",
          boxShadow: "0 0 0 1px rgba(255, 255, 255, 0.3)",
          boxSizing: "border-box",
          transition: "all 0.05s ease-out",
        }}
      />

      {/* DevTools 悬停信息卡片 */}
      <div
        className="devtools-inspector-tip"
        style={{
          position: "absolute",
          left: tipX,
          top: tipY,
          width: TIP_W,
          pointerEvents: "none",
          zIndex: 50,
          background: "var(--bg-elevated, #1e1e2e)",
          color: "var(--text-1, #e0e0e0)",
          border: "1px solid var(--border, rgba(255, 255, 255, 0.15))",
          borderRadius: 6,
          padding: "8px 10px",
          boxShadow: "0 8px 24px rgba(0, 0, 0, 0.35)",
          fontSize: 11,
          fontFamily: "var(--font-sans, -apple-system, sans-serif)",
          lineHeight: 1.4,
          backdropFilter: "blur(8px)",
        }}
      >
        {/* 标题行：tag#id.classes */}
        <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4, fontFamily: "var(--font-mono, monospace)" }}>
          <span style={{ color: "#38bdf8", fontWeight: 700 }}>&lt;{info.tag}&gt;</span>
          {info.id && <span style={{ color: "#f59e0b" }}>#{info.id}</span>}
          {info.className && (
            <span style={{ color: "#94a3b8", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
              .{info.className.split(" ").filter(Boolean).slice(0, 3).join(".")}
            </span>
          )}
        </div>

        {/* 基础属性网格 */}
        <div style={{ display: "grid", gridTemplateColumns: "50px 1fr", gap: "2px 8px", fontSize: 10 }}>
          <span style={{ color: "var(--text-3, #888)" }}>尺寸</span>
          <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600, color: "var(--text-1, #fff)" }}>
            {info.width} × {info.height} px
          </span>

          {info.color && (
            <>
              <span style={{ color: "var(--text-3, #888)" }}>文字色</span>
              <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                <span style={{ width: 9, height: 9, borderRadius: 2, background: info.color, border: "1px solid rgba(255,255,255,0.2)", display: "inline-block" }} />
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>{info.color}</span>
              </div>
            </>
          )}

          {info.backgroundColor && info.backgroundColor !== "rgba(0, 0, 0, 0)" && info.backgroundColor !== "transparent" && (
            <>
              <span style={{ color: "var(--text-3, #888)" }}>背景色</span>
              <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                <span style={{ width: 9, height: 9, borderRadius: 2, background: info.backgroundColor, border: "1px solid rgba(255,255,255,0.2)", display: "inline-block" }} />
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>{info.backgroundColor}</span>
              </div>
            </>
          )}

          {info.fontSize && (
            <>
              <span style={{ color: "var(--text-3, #888)" }}>字体</span>
              <span style={{ fontFamily: "var(--font-mono)" }}>{info.fontSize}</span>
            </>
          )}

          {(info.role || info.ariaLabel || info.placeholder) && (
            <>
              <span style={{ color: "var(--text-3, #888)" }}>无障碍</span>
              <span style={{ color: "#10b981", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {info.role ? `role="${info.role}" ` : ""}{info.ariaLabel || info.placeholder || ""}
              </span>
            </>
          )}
        </div>

        {info.text && (
          <div style={{ marginTop: 5, paddingTop: 4, borderTop: "1px solid var(--border, rgba(255,255,255,0.1))", fontSize: 10, color: "var(--text-2, #bbb)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            "{info.text}"
          </div>
        )}
      </div>
    </>
  );
});
