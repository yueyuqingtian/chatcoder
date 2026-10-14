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

/** 下载进度：百分比为主读数，大小与速度作次要信息（tabular 数字，跳动时不抖动布局） */
export function UpdateProgress({ percent, transferred, total, bytesPerSecond }: {
  percent: number; transferred?: number; total?: number; bytesPerSecond?: number;
}) {
  const p = Math.max(0, Math.min(100, Math.round(percent || 0)));
  const size = total ? `${fmtBytes(transferred) || "0 KB"} / ${fmtBytes(total)}` : "";
  const speed = bytesPerSecond ? `${fmtBytes(bytesPerSecond)}/s` : "";
  return (
    <div className="upd-prog">
      <div className="upd-prog-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={p}>
        <div className="upd-prog-fill" style={{ width: `${p}%` }} />
      </div>
      <div className="upd-prog-meta">
        <span className="upd-prog-pct">{p}%</span>
        <span className="upd-prog-sub">{[size, speed].filter(Boolean).join(" · ")}</span>
      </div>
    </div>
  );
}
