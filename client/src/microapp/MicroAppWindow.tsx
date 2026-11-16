/** 独立窗口里的微应用承载层（plan：拓展页 / 右侧面板「在新窗口打开」）。
 *
 *  三种刻意的取舍：
 *  ① webview 的 `partition` / src 形状与右侧面板里的 `MicroAppPanel` **逐字一致**
 *     （`persist:microapp` + `app://microapp/<appId>/<version>/<entry>?uid=`）：主进程的
 *     `hardenWebviewAttach` 与宿主桥寻址都按这两点判定，形状不同就等于另一个应用实例。
 *  ② 全屏只有一条路：主进程用 F11 切换、Esc 退出；窗口内再给一个图标按钮（外置微应用页面
 *     没有宿主 UI，那种窗口只能靠快捷键，所以这里说明里两句都写上）。
 *  ③ 不在这个窗口里申请宿主桥身份：uid → sessionId 的绑定由主窗口渲染层持有
 *     （见 client/src/utils/microappWindow.ts），否则每个窗口各绑一份会让上行请求分叉。
 */
import { useEffect, useState } from "react";
import { IconMaximize } from "../components/icons";
// 直接引 ui/Tooltip 而不是 ui 桶文件：本窗口只用得到提示浮层，走桶文件会把整卷组件库
// （含 Markdown / Monaco 相关样式）拖进这个独立入口的产物里。
import { Tooltip, TooltipProvider } from "../components/ui/Tooltip";

/** webview 加载地址：与 MicroAppPanel 的 bridgeSrc 同一份形状（uid 必须 encodeURIComponent，
 *  否则 `microapp#3` 里的 `#` 会被当成 fragment 起点，宿主侧 uid 被截断成 `microapp`）。 */
function bridgeSrc(appId: string, version: string, entry: string, uid: string): string {
  const base = `app://microapp/${appId}/${version}/${entry || "app/index.html"}`;
  if (!uid) return base;
  return `${base}${base.includes("?") ? "&" : "?"}uid=${encodeURIComponent(uid)}`;
}

export function MicroAppWindow({ appId, version, entry, uid, name }: {
  appId: string;
  version: string;
  entry: string;
  uid: string;
  name: string;
}) {
  const [fullscreen, setFullscreen] = useState(false);

  // 全屏态由主进程回传（F11 / Esc / 按钮三条路径都能改它，只有主进程知道真值）
  useEffect(() => {
    const off = window.chatcoderAPI?.onMicroAppWindowFullscreen?.((on) => setFullscreen(on === true));
    return () => { off?.(); };
  }, []);

  const toggleFullscreen = () => {
    void window.chatcoderAPI?.toggleMicroAppWindowFullscreen?.();
  };

  const ready = Boolean(appId && version);
  // TooltipProvider 提供统一浮层宿主：主窗的宿主在 App 里挂，这个独立入口必须自己挂一份，
  // 否则工具条按钮的提示浮层没有落点（点了/悬停了什么都不出现）。
  return (
    <TooltipProvider>
      <div className="maw-root">
        {ready ? (
          <webview
            src={bridgeSrc(appId, version, entry, uid)}
            className="maw-frame"
            {...({ partition: "persist:microapp", "data-uid": uid } as Record<string, string>)}
          />
        ) : (
          <div className="maw-empty">
            <div className="maw-empty-title">没有绑定微应用</div>
            <div className="maw-empty-desc">请从「设置 → 拓展」或右侧面板的微应用列表里重新打开。</div>
          </div>
        )}
        <div className="maw-tools">
          <Tooltip content={fullscreen ? `${name || "微应用"} · 退出全屏（Esc）` : `${name || "微应用"} · 全屏（F11）`} side="left">
            <button
              type="button"
              className="maw-btn"
              aria-label={fullscreen ? "退出全屏" : "全屏"}
              aria-pressed={fullscreen}
              onClick={toggleFullscreen}
            >
              <IconMaximize size={14} />
            </button>
          </Tooltip>
        </div>
      </div>
    </TooltipProvider>
  );
}
