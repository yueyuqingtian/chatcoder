/** 升级后首启"本次更新"弹窗（plan-230-1144 M4.2）。
 *
 * 在应用升级后首次启动时展示「从上次看到的版本 → 当前版本」之间的全部 Release Notes
 * （GitHub Releases 拉取，离线回落状态机携带的 notes）。展示时机由主进程 `consumeWhatsNew`
 * 判定（lastSeenVersion < appVersion 时为 true，消费后落盘），同一版本只弹一次。
 *
 * plan-405-1900：此前是「版本胶囊 + 卡片套卡片 + 原样 Markdown」，信息被框线切得很碎。
 * 现改为：头部用应用图标 + 「已更新到 vX」交代结果；正文按版本分段（仅跨多个版本时显示段头），
 * 每段与关于页共用 ReleaseNotesView 的「分组标签 + 条目」排版；滚动只交给弹窗 body。 */
import { useUpdaterStore } from "../store/updater";
import { AppLogo } from "./AppLogo";
import { Button, Dialog } from "./ui";
import { ReleaseNotesView } from "./update/ReleaseNotes";

export function WhatsNewModal() {
  const pending = useUpdaterStore((s) => s.pendingWhatsNew);
  const appVersion = useUpdaterStore((s) => s.appVersion);
  const dismiss = useUpdaterStore((s) => s.dismissWhatsNew);

  const releases = pending || [];
  const open = releases.length > 0;
  const version = appVersion || releases[0]?.version || "";
  const multi = releases.length > 1;
  return (
    <Dialog
      open={open}
      onClose={dismiss}
      width={600}
      className="whatsnew-dialog"
      title={
        <span className="wn-hero">
          <AppLogo size={44} />
          <span className="wn-hero-text">
            <span className="wn-hero-title">已更新到 {version ? `v${version}` : "新版本"}</span>
            <span className="wn-hero-sub">{multi ? `以下是最近 ${releases.length} 个版本带来的变化` : "以下是这个版本带来的变化"}</span>
          </span>
        </span>
      }
      footer={<Button variant="primary" size="sm" onClick={dismiss}>开始使用</Button>}
    >
      <div className="wn-body">
        {releases.map((r, i) => (
          <section key={`${r.version}-${i}`} className="wn-rel">
            {multi && (
              <div className="wn-rel-head">
                <span className="wn-rel-ver">{r.version ? `v${r.version}` : "更新说明"}</span>
                {r.date && <span className="wn-rel-date">{new Date(r.date).toLocaleDateString()}</span>}
              </div>
            )}
            <ReleaseNotesView notes={r.notes} empty="这个版本没有附带更新说明" />
          </section>
        ))}
      </div>
    </Dialog>
  );
}
