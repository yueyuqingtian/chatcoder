import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * v1.0: 全局错误边界 — 捕获子组件树的运行时错误，展示友好提示。
 * 包裹在 App 顶层，防止单个组件崩溃导致整个应用白屏。
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("[ErrorBoundary] 捕获错误:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
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
          <div style={{ fontSize: "48px" }}>⚠️</div>
          <h1 style={{ fontSize: "20px", fontWeight: 600, margin: 0 }}>
            应用遇到意外错误
          </h1>
          <p style={{ fontSize: "14px", color: "var(--text-muted, #666)", maxWidth: "500px", textAlign: "center" }}>
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
