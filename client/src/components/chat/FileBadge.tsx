/** 文件类型徽标（对齐 zcode：JS 黄 / TS 蓝 / Vue 绿 / CSS 紫 小方块） */
export function FileBadge({ path, size = 18 }: { path: string; size?: number }) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  const map: Record<string, { label: string; bg: string; fg: string }> = {
    js: { label: "JS", bg: "#f7df1e", fg: "#7a6500" },
    jsx: { label: "JS", bg: "#f7df1e", fg: "#7a6500" },
    mjs: { label: "JS", bg: "#f7df1e", fg: "#7a6500" },
    cjs: { label: "JS", bg: "#f7df1e", fg: "#7a6500" },
    ts: { label: "TS", bg: "#3178c6", fg: "#fff" },
    tsx: { label: "TS", bg: "#3178c6", fg: "#fff" },
    vue: { label: "V", bg: "#41b883", fg: "#fff" },
    css: { label: "CS", bg: "#a074c4", fg: "#fff" },
    scss: { label: "CS", bg: "#a074c4", fg: "#fff" },
    py: { label: "PY", bg: "#3776ab", fg: "#fff" },
    json: { label: "{}", bg: "#8b8b8b", fg: "#fff" },
    md: { label: "M", bg: "#6b7280", fg: "#fff" },
    html: { label: "<>", bg: "#e34c26", fg: "#fff" },
    go: { label: "GO", bg: "#00add8", fg: "#fff" },
    rs: { label: "RS", bg: "#dea584", fg: "#3b2b20" },
  };
  const it = map[ext] ?? { label: ext.slice(0, 2).toUpperCase() || "·", bg: "var(--bg-hover)", fg: "var(--text-2)" };
  return (
    <span
      className="file-badge"
      style={{ background: it.bg, color: it.fg, width: size, height: size, fontSize: size * 0.44 }}
    >
      {it.label}
    </span>
  );
}

/** 拆分路径为目录 + 文件名 */
export function splitFilePath(path: string): { dir: string; name: string } {
  const clean = path.replace(/\\/g, "/");
  const i = clean.lastIndexOf("/");
  if (i < 0) return { dir: "", name: clean };
  return { dir: clean.slice(0, i + 1), name: clean.slice(i + 1) };
}
