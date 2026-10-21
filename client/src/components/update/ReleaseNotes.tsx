/** 更新内容展示的共用件（plan-405-1900）
 *
 * 关于页「新版本内容 / 更新历史」与升级后首启弹窗此前各写一套：一处是彩色圆形图标 + 分组标题，
 * 一处是卡片套卡片的原样 Markdown，同一份更新说明在三处长得都不一样。
 * 现统一为「左列分组标签 + 右列条目」的两栏排版：分组靠文字与颜色区分，不再叠图标与角标；
 * 不符合「### 分组 + - 条目」结构的说明（如 GitHub 自由格式）回落 Markdown 原样渲染。 */
import { MarkdownContent } from "../MarkdownContent";

export type ReleaseTone = "add" | "opt" | "fix" | "warn";
export interface ReleaseGroup { title: string; tone: ReleaseTone; items: string[] }

/** 把 CHANGELOG 版本块按「### 分组 + - 条目」解析为结构化分组；
 *  不符合该结构（无分组或含其他段落）时返回 null，回落 Markdown 原样渲染。 */
export function parseReleaseGroups(notes?: string): ReleaseGroup[] | null {
  if (!notes) return null;
  const groups: ReleaseGroup[] = [];
  for (const raw of notes.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const h = /^#{2,4}\s+(.+)$/.exec(line);
    if (h) {
      const title = h[1].trim();
      const tone: ReleaseTone = /新增|feat|add/i.test(title) ? "add" : /修复|fix/i.test(title) ? "fix" : /重要|break/i.test(title) ? "warn" : "opt";
      groups.push({ title, tone, items: [] });
      continue;
    }
    const li = /^[-*]\s+(.+)$/.exec(line);
    if (li && groups.length > 0) { groups[groups.length - 1].items.push(li[1]); continue; }
    return null;
  }
  const valid = groups.filter((g) => g.items.length > 0);
  return valid.length > 0 ? valid : null;
}

/** 条目数（只数 "- 条目"，分组标题不计） */
export function countReleaseItems(notes?: string): number {
  return (notes || "").split("\n").filter((l) => /^\s*[-*]\s+/.test(l)).length;
}

export function ReleaseNotesView({ notes, empty = "暂无更新说明" }: { notes?: string; empty?: string }) {
  const groups = parseReleaseGroups(notes);
  if (!notes?.trim()) return <div className="rn-empty">{empty}</div>;
  if (!groups) {
    return <div className="rn-raw release-notes"><MarkdownContent>{notes}</MarkdownContent></div>;
  }
  return (
    <div className="rn-groups">
      {groups.map((g) => (
        <section key={g.title} className={"rn-group tone-" + g.tone}>
          <div className="rn-tag">{g.title}</div>
          <ul className="rn-list release-notes">
            {g.items.map((it, j) => <li key={j}><MarkdownContent>{it}</MarkdownContent></li>)}
          </ul>
        </section>
      ))}
    </div>
  );
}

function fmtBytes(n?: number): string {
  if (!n || n <= 0) return "";
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 剩余时间：不足 1 分钟写秒，不足 1 小时写分秒，其余写时分（只在有速度时给） */
function fmtEta(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

/** 下载进度：百分比为主读数，大小 / 速度 / 剩余时间作次要信息（tabular 数字，跳动时不抖动布局）。
 *  plan-20261021-usage-update：「连接中」没有进度可言，用不定长滑块而不是 0% 的空条；
 *  断网 / 重试时保留已下载的百分比，只把条调成静止的弱色，避免看起来像卡死或归零。 */
export function UpdateProgress({ percent, transferred, total, bytesPerSecond, phase, source }: {
  percent: number; transferred?: number; total?: number; bytesPerSecond?: number;
  phase?: "connecting" | "downloading" | "waiting" | "retrying" | "verifying"; source?: string;
}) {
  const p = Math.max(0, Math.min(100, Math.round(percent || 0)));
  const connecting = phase === "connecting";
  const stalled = phase === "waiting" || phase === "retrying";
  const size = total ? `${fmtBytes(transferred) || "0 KB"} / ${fmtBytes(total)}` : "";
  const flowing = !stalled && !connecting && phase !== "verifying";
  const speed = flowing && bytesPerSecond ? `${fmtBytes(bytesPerSecond)}/s` : "";
  const eta = flowing && bytesPerSecond && total && transferred != null && total > transferred
    ? `剩余 ${fmtEta((total - transferred) / bytesPerSecond)}` : "";
  const sub = connecting ? "" : [size, speed, eta, flowing ? source : ""].filter(Boolean).join(" · ");
  return (
    <div className="upd-prog">
      <div
        className={"upd-prog-track" + (connecting ? " is-indeterminate" : "") + (stalled ? " is-stalled" : "")}
        role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={connecting ? undefined : p}
      >
        <div className="upd-prog-fill" style={connecting ? undefined : { width: `${p}%` }} />
      </div>
      <div className="upd-prog-meta">
        <span className="upd-prog-pct">{connecting ? "连接中…" : `${p}%`}</span>
        <span className="upd-prog-sub">{sub}</span>
      </div>
    </div>
  );
}
