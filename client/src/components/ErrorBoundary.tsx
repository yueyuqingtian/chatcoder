import { Component, type ErrorInfo, type ReactNode } from "react";
import { IconAlertTriangle } from "./icons";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  /**
   * 变更时自动清除错误状态（用于可切换的局部区域，如右面板切 tab）。
   * 未提供时保持原有行为：一旦出错需整页重载恢复。
   */
  resetKey?: unknown;
  /** page=整页兜底（全屏居中）；panel=局部区域兜底（紧凑卡片，不挤占整页） */
  variant?: "page" | "panel";
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * v1.0: 全局错误边界 — 捕获子组件树的运行时错误，展示友好提示。
 * 包裹在 App 顶层，防止单个组件崩溃导致整个应用白屏。
 *
 * v0.5.8: 新增 resetKey 与 variant。右面板内的局部区域（如浏览器面板）二次包裹，
 * 避免单块面板的异常把整个应用打成白屏——顶层边界只负责最后的兜底。
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidUpdate(prevProps: Props) {
    // resetKey 变化（切换 tab / 会话）时自动复位，无需用户手动重载整页
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false, error: null });
    }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("[ErrorBoundary] 捕获错误:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      if (this.props.variant === "panel") {
        return (
          <div
            role="alert"
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              gap: "10px",
              padding: "24px 16px",
              textAlign: "center",
              fontFamily: "var(--font-sans, system-ui)",
              color: "var(--text-2)",
            }}
          >
            <div style={{ display: "flex", color: "var(--warning)" }}>
              <IconAlertTriangle size={28} strokeWidth={1.5} />
            </div>
            <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-1)" }}>此面板出现异常</div>
            <div style={{ fontSize: "12px", lineHeight: 1.5, maxWidth: "320px", overflowWrap: "anywhere" }}>
              {this.state.error?.message || "未知错误"}
            </div>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => this.setState({ hasError: false, error: null })}
            >
              重试
            </button>
          </div>
        );
      }
      return (
        <div
          role="alert"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            gap: "16px",
            padding: "32px",
            fontFamily: "var(--font-sans, system-ui)",
            color: "var(--text, #333)",
            background: "var(--bg, #fff)",
          }}
        >
          <div style={{ display: "flex", color: "var(--warning)" }}><IconAlertTriangle size={48} strokeWidth={1.5} /></div>
          <h1 style={{ fontSize: "20px", fontWeight: 600, margin: 0 }}>
            应用遇到意外错误
          </h1>
          <p style={{ fontSize: "14px", color: "var(--text-2)", maxWidth: "500px", textAlign: "center" }}>
            {this.state.error?.message || "未知错误"}
          </p>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: "8px 20px",
              borderRadius: "8px",
              border: "1px solid var(--border, #ddd)",
              background: "var(--accent-dim, #f0f0ff)",
              color: "var(--accent, #4f46e5)",
              cursor: "pointer",
              fontSize: "14px",
            }}
          >
            重新加载
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
