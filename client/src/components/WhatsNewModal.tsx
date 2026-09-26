/** 升级后首启"本次更新"弹窗（plan-230-1144 M4.2）。
 *
 * 此前更新前后都看不到版本变更内容（问题6）：主进程 `update-available` 回调
 * 丢弃了 releaseNotes，渲染侧也没有任何展示入口。本弹窗在应用升级后首次启动
 * 时展示「从上次看到的版本 → 当前版本」之间的全部 Release Notes（GitHub
 * Releases 拉取，离线回落状态机携带的 notes）。
 *
 * 展示时机由主进程 `consumeWhatsNew` 判定（lastSeenVersion < appVersion 时为 true，
 * 消费后落盘），确保同一版本只弹一次。
 *
 * plan-283-1428 排版：多版本说明此前是「版本行 + 无分隔的连续 Markdown」，
 * 长说明通篇平铺、条目之间没有视觉切分；且 .whatsnew-body 自带 max-height
 * 滚动，嵌在同样可滚动的弹窗 body 内形成双滚动条。现改为：
 *  - 每个版本一张卡片（版本徽标 + 日期头部 + 分隔线 + 内容区），条目边界清晰；
 *  - 去掉内层滚动，滚动统一交给弹窗 body，不再出现双滚动条与滚动穿透。 */
import { useUpdaterStore } from "../store/updater";
import { Modal } from "./Modal";
import { MarkdownContent } from "./MarkdownContent";

export function WhatsNewModal() {
  const pending = useUpdaterStore((s) => s.pendingWhatsNew);
  const appVersion = useUpdaterStore((s) => s.appVersion);
  const dismiss = useUpdaterStore((s) => s.dismissWhatsNew);

  const releases = pending || [];
  const open = releases.length > 0;
  return (
    <Modal
      open={open}
      onClose={dismiss}
      title="本次更新内容"
      subtitle={appVersion ? `当前版本 v${appVersion}` : undefined}
      width={640}
      footer={
        <button className="btn btn-primary btn-sm" onClick={dismiss}>知道了</button>
      }
    >
      <div className="whatsnew-body">
        {/* 多版本时先给出行数概览，避免用户逐条猜测本次跨度 */}
        {releases.length > 1 && (
          <div className="whatsnew-summary">含 {releases.length} 个版本的更新说明</div>
        )}
        {releases.map((r, i) => (
          <div key={`${r.version}-${i}`} className="whatsnew-item">
            <div className="whatsnew-ver">
              <span className="whatsnew-ver-badge">{r.version ? `v${r.version}` : "更新说明"}</span>
              {r.date && <span className="whatsnew-date">{new Date(r.date).toLocaleDateString()}</span>}
            </div>
            <div className="whatsnew-notes release-notes">
              <MarkdownContent>{r.notes || "(无说明)"}</MarkdownContent>
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}
