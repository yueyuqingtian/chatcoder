/** 微应用面板（plan-693-2868 P0-4）：右侧面板里承载单个微应用的界面。
 *
 *  三个刻意的取舍：
 *  ① 独立 partition（persist:microapp）——浏览器面板那个 session 里堆着网页缓存与站点权限，
 *     微应用不该共享；分开之后 CSP、存储、Cookie 互不影响，关掉微应用也不会波及浏览器。
 *  ② 只加载 app:// 自定义协议——页面由主进程 protocol.handle 给出，响应头即带 CSP
 *     （connect-src 'none'），装进来的微应用拿不到网络出口。
 *  ③ 隐藏用 CSS 而不是卸载——切标签 / 收起面板后 AI 仍可能在调用它，卸载重载会丢界面状态。
 *
 *  plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§6（宿主桥 v2）：
 *  这个组件是微应用界面在渲染层的**身份来源**。页面要靠 `?uid=` 认出自己是哪个标签
 *  （preload 读 location.search → instance.uid），主进程也要靠 URL 里的 uid 建立
 *  uid → guest 映射，否则同 appId 开两个标签时上行请求与下行日志会串台。
 *  上行 / 下行的实际转发集中在 `browserAgent/microappHost.ts`（单例）：本组件只负责
 *  ① 把 `uid + 标签所属会话桶` 交给它（`bind`），② 确保那一跳已启动（`start`，幂等）。
 *  为什么不在组件里直接订阅 IPC：面板会随标签反复挂载，多个面板各自订阅会让同一条请求
 *  被转发多次（命令执行多遍、页面收到多份回执）。
 */
import { useEffect } from "react";
import { microappHost } from "../../browserAgent/microappHost";
import { openMicroAppWindow } from "../../utils/microappWindow";
import { IconExternalLink } from "../icons";
import { Tooltip } from "../ui";

/* 桌面端判据：preload 暴露的是 `window.chatcoderAPI`（见 electron/preload.cjs:4 与
   types/global.d.ts），不是 `window.chatcoder`——后者永远不存在，会恒走浏览器分支，
   在桌面端也显示「需要在桌面端运行」（plan-693-2868 回归修复）。 */
const isElectron = typeof window !== "undefined"
  && Boolean((window as unknown as { chatcoderAPI?: unknown }).chatcoderAPI);

/** webview 的加载地址：`app://microapp/<appId>/<version>/<entry>?uid=<标签 uid>`。
 *
 *  uid 必须 `encodeURIComponent`：store 分配的 uid 形如 `microapp#3`，裸 `#` 会被当成
 *  fragment 起点，uid 被截断成 `microapp`——同 appId 的多个标签在宿主侧就撞成一个
 *  （协议 §6 要修的正是这个串台）。主进程与 preload 两侧都补了「裸 #数字」的兼容规则，
 *  这里按正确方式编码，双保险。
 *
 *  plan-external-microapps：`externalUrl` 有值时是**外置微应用**——它自带前后端与进程，
 *  直接加载它自己的 http 地址，不经 `app://` 包装、不拼 uid（它不参与宿主桥寻址）。
 */
function bridgeSrc(appId: string, version: string, entry: string | undefined, uid: string, externalUrl?: string): string {
  if (externalUrl) return externalUrl;
  const base = `app://microapp/${appId}/${version}/${entry ?? "app/index.html"}`;
  if (!uid) return base;
  return `${base}${base.includes("?") ? "&" : "?"}uid=${encodeURIComponent(uid)}`;
}

export function MicroAppPanel({ appId, version, entry, visible, uid = "", sessionId = null, externalUrl = "" }: {
  appId: string;
  version: string;
  entry?: string;
  visible: boolean;
  /** 本标签的唯一标识（`store/panel.ts` 的 nextUid 产出，形如 `microapp#3`）；宿主桥按它寻址实例 */
  uid?: string;
  /** 标签**所属会话桶**的会话 id（`bucketSessionId(桶key)`，不是当前活动会话）：
   *  后台会话里的微应用必须把命令跑进它自己的项目，不能落到用户正在看的会话上。 */
  sessionId?: number | null;
  /** plan-external-microapps：外置微应用的前端地址；给了就直连它，不注入宿主桥 */
  externalUrl?: string;
}) {
  const external = Boolean(externalUrl);
  const src = bridgeSrc(appId, version, entry, uid, externalUrl || undefined);
  // 空态**不再走早返回**：它必须和 webview 共用同一个 viewport 容器才拿得到满高，
  // 否则未绑定时又回到「只看得到一条」的样子（plan-693-2868 回归修复）。
  const ready = Boolean(appId && version) || external;

  // 宿主桥接线（协议 §4-§6）：启动那一跳 + 登记本界面的身份。
  // 两个调用都幂等：start 是引用计数（重挂载不叠加监听），bind 返回的解绑只摘自己那一次。
  // 外置微应用不参与宿主桥（它有自己的后端），跳过绑定——否则主进程收不到它的 guest，
  // 渲染层还会因为 uid 未绑定而把请求全部拒掉。
  useEffect(() => {
    if (!ready || !uid || external) return;
    const stop = microappHost.start();
    const unbind = microappHost.bind({ uid, sessionId, appId, version });
    return () => {
      unbind();
      stop();
    };
  }, [ready, uid, sessionId, appId, version, external]);

  // 三种状态共用同一个 viewport 容器：这样空态、webview、未绑定都能拿到满高，
  // 不会出现「只看得到一条」的坍缩（容器高度见 global.css 的 .microapp-viewport）。
  return (
    <div className="microapp-viewport" style={{ display: visible ? "flex" : "none" }}>
      {!ready ? (
        <div className="microapp-empty">
          <div className="microapp-empty-title">没有绑定微应用</div>
          <div className="microapp-empty-desc">从输入框的微应用菜单或「拓展」页注入一个后再打开。</div>
        </div>
      ) : isElectron ? (
        <>
          <webview
            src={src}
            className="microapp-frame"
            // data-uid 是与主进程对齐的可读标识（调试时从元素上一眼看到实例身份）；
            // 真正参与寻址的是 URL 里的 ?uid=，两者必须一致。外置微应用没有 uid，留空。
            {...({ partition: "persist:microapp", "data-uid": uid, "data-external": external ? "1" : "0" } as Record<string, string>)}
          />
          {/* 面板内的标签照旧可用（AI 仍可在面板里操作它）；这里只额外给一条「在独立窗口里看」的路：
              同一个应用同时存在面板标签与窗口实例时用两个不同的 uid，互不抢地址。
              窗口里的实例仍属于**这个标签所属的会话**（sessionId 一并带过去），宿主桥不会断。 */}
          <div className="microapp-tools">
            <Tooltip content="在独立窗口里打开（仍属于当前会话）" side="left">
              <button
                type="button"
                className="ui-icon-btn size-xs"
                aria-label="在独立窗口里打开"
                onClick={() => void openMicroAppWindow({ appId, version, entry, uid, sessionId, externalUrl, name: appId })}
              >
                <IconExternalLink size={14} />
              </button>
            </Tooltip>
          </div>
        </>
      ) : (
        <div className="microapp-empty">
          <div className="microapp-empty-title">需要在桌面端运行</div>
          <div className="microapp-empty-desc">
            {external
              ? "外置微应用依赖 Electron 的 webview 承载；也可以在系统浏览器里直接打开它的地址。"
              : "内置微应用依赖 Electron 的 app:// 协议，浏览器里没有这个通道。"}
          </div>
        </div>
      )}
    </div>
  );
}
