/**
 * 截图卡（plan-20261015-browser-agent）：浏览器截图、电脑操控截图、「动作后附带截图」的结果预览。
 *
 * - 位置固定在产生截图的那次工具调用之后，随「工作过程」一起折叠（汇报之后的截图仍常显），刷新回放位置不变；
 * - 运行中先占同尺寸的槽位（高度固定），图到达后原位替换，不推动下方内容；
 * - 预览尺寸按截图比例推算、限定在小图范围内，避免占满消息列；
 * - 点击预览图打开全局图片查看器，可在本轮的截图之间左右切换。
 */
import { memo } from "react";
import type { ToolLeaf } from "./timeline";
import { useChatStore, type ToolImage } from "../../store/chat";
import { resolveFileUrl } from "../../api/client";
import { openGallery } from "../../store/gallery";
import { usePanelStore } from "../../store/panel";
import { useBrowserStore } from "../../store/browser";
import { IconCamera, IconMonitor, IconGlobe } from "../icons";
import { Tooltip } from "../ui";

/** 预览高度固定（运行中占位与最终图同高）；宽度由截图比例推算，限定在 THUMB_MIN_W~THUMB_MAX_W 之间 */
const THUMB_H = 150;
const THUMB_MIN_W = 120;
const THUMB_MAX_W = 240;

function thumbWidth(ratio: number): number {
  return Math.round(Math.min(THUMB_MAX_W, Math.max(THUMB_MIN_W, THUMB_H * ratio)));
}

function shotLabel(tool: string): string {
  if (tool === "browser_screenshot") return "浏览器截图";
  if (tool === "desktop_screenshot") return "屏幕截图";
  if (tool.startsWith("browser_")) return "操作后的页面";
  return "操作后的画面";
}

function hostOf(url?: string): string {
  if (!url) return "";
  try { return new URL(url).host; } catch { return ""; }
}

function liveImage(leaf: ToolLeaf): ToolImage | null {
  return useChatStore.getState().runningToolResults[leaf.callKey]?.image ?? leaf.image;
}

export const ToolShotCard = memo(function ToolShotCard({ leaf, siblings }: {
  leaf: ToolLeaf;
  /** 本轮全部截图卡（大图查看器左右切换用） */
  siblings: ToolLeaf[];
}) {
  const live = useChatStore((s) => s.runningToolResults[leaf.callKey]);
  const image = live?.image ?? leaf.image;
  const running = leaf.ok === null && !live;
  const failed = (leaf.ok === false || live?.ok === false) && !image;
  if (failed) return null;
  if (!image && !running) return null;

  const isBrowser = leaf.tool.startsWith("browser_");
  const source = image?.title || hostOf(image?.page) || "";
  const ratio = image?.width && image?.height ? image.width / image.height : 16 / 10;
  const thumbStyle = { width: thumbWidth(ratio), height: THUMB_H };

  const openLarge = () => {
    const list: Array<{ url: string; name: string }> = [];
    let index = 0;
    for (const l of siblings) {
      const img = liveImage(l);
      if (!img) continue;
      if (l.callKey === leaf.callKey) index = list.length;
      list.push({ url: resolveFileUrl(img.url), name: `${shotLabel(l.tool)}-${l.callKey.slice(-6)}.jpg` });
    }
    if (list.length === 0 && image) list.push({ url: resolveFileUrl(image.url), name: "截图.jpg" });
    openGallery(list, index);
  };

  const revealInBrowser = () => {
    const panel = usePanelStore.getState();
    panel.openPanel();
    panel.openTab("browser");
    const sid = useChatStore.getState().currentSessionId;
    if (image?.page) {
      const store = useBrowserStore.getState();
      const hit = store.getSessionState(sid).tabs.find((t) => t.url === image.page);
      if (hit) store.setActiveTab(sid, hit.id);
    }
  };

  return (
    <div className="shot-card">
      <div className="shot-head">
        <span className="shot-head-ic">{leaf.tool === "desktop_screenshot" ? <IconMonitor size={14} /> : <IconCamera size={14} />}</span>
        <span className="shot-head-label">{running ? "正在截图…" : shotLabel(leaf.tool)}</span>
        {source && (
          <Tooltip content={image?.page || source}>
            <span className="shot-head-src">{source}</span>
          </Tooltip>
        )}
        {isBrowser && !running && (
          <button type="button" className="shot-head-link" onClick={revealInBrowser}>
            <IconGlobe size={12} />在浏览器中查看
          </button>
        )}
      </div>
      {image ? (
        <button
          type="button"
          className="shot-thumb"
          style={thumbStyle}
          onClick={openLarge}
          aria-label="查看大图"
        >
          <img src={resolveFileUrl(image.url)} alt={shotLabel(leaf.tool)} loading="lazy" decoding="async" draggable={false} />
        </button>
      ) : (
        <div className="shot-thumb is-loading" style={thumbStyle} aria-hidden />
      )}
    </div>
  );
});
