/** 升级后首启"本次更新"弹窗（plan-230-1144 M4.2）。
 *
 * 此前更新前后都看不到版本变更内容（问题6）：主进程 `update-available` 回调
 * 丢弃了 releaseNotes，渲染侧也没有任何展示入口。本弹窗在应用升级后首次启动
 * 时展示「从上次看到的版本 → 当前版本」之间的全部 Release Notes（GitHub
 * Releases 拉取，离线回落状态机携带的 notes）。
 *
 * 展示时机由主进程 `consumeWhatsNew` 判定（lastSeenVersion < appVersion 时为 true，
 * 消费后落盘），确保同一版本只弹一次。 */
import { useUpdaterStore } from "../store/updater";
import { Modal } from "./Modal";
import { MarkdownContent } from "./MarkdownContent";

export function WhatsNewModal() {
  const pending = useUpdaterStore((s) => s.pendingWhatsNew);
  const appVersion = useUpdaterStore((s) => s.appVersion);
  const dismiss = useUpdaterStore((s) => s.dismissWhatsNew);

  const open = Array.isArray(pending) && pending.length > 0;
  return (
    <Modal
      open={open}
      onClose={dismiss}
      title="本次更新内容"
      subtitle={appVersion ? `当前版本 v${appVersion}` : undefined}
      width={640}
      actions={
        <button className="btn btn-primary btn-sm" onClick={dismiss}>知道了</button>
      }
    >
      <div className="whatsnew-body">
        {(pending || []).map((r, i) => (
          <div key={`${r.version}-${i}`} className="whatsnew-item release-notes">
            <div className="whatsnew-ver">
              <span className="whatsnew-ver-badge">{r.version ? `v${r.version}` : "更新说明"}</span>
              {r.date && <span className="whatsnew-date">{new Date(r.date).toLocaleDateString()}</span>}
            </div>
            <MarkdownContent>{r.notes || "(无说明)"}</MarkdownContent>
          </div>
        ))}
      </div>
    </Modal>
  );
}
