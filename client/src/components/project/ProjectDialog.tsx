/** 新建 / 编辑项目弹窗（plan-20261009-ui U1，参考图1/图2）。
 *
 * 用户路径：点「新建项目」→ 第一眼就是「源文件夹」投放区——把文件夹拖进弹窗任意位置，或点击后选择；
 * 选好后名称自动带出（可改）→ 挑图标与颜色（预览块与网格实时着色）→ 决定是否建立索引 → 创建。
 * 全程不需要手敲路径：
 *   · 桌面版   点击走系统选择器；拖入走 preload 的 webUtils.getPathForFile 取绝对路径；
 *   · 浏览器   点击展开本机文件夹浏览器（后端代为列目录）；拖入时浏览器读不到路径，由后端按「名称 + 子项」定位。
 * 编辑模式：路径只读（后端 projects.path 创建后不可改），可改名称 / 图标 / 颜色 / 索引开关。
 *
 * 由 App 挂载单例，通过 store/projectDialog 打开（侧栏、输入框工作区选择器、项目菜单共用）。 */
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, ApiError } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { useProjectDialogStore } from "../../store/projectDialog";
import { Button, Dialog, DialogSection, InsetCard, Switch, Tooltip } from "../ui";
import { IconFolderPlus, IconX, IconRefresh } from "../icons";
import { FileIcon, baseName } from "../iconkit/FileIcon";
import { FolderBrowser } from "./FolderBrowser";
import { ProjectIcon } from "./ProjectIcon";
import { PROJECT_ICON_DEFS } from "./projectIconData";
import { PROJECT_COLORS, DEFAULT_PROJECT_ICON, projectColorKey, projectIconKey, suggestProjectColor } from "./projectMeta";

/** 索引库按工作区路径登记；比较时统一斜杠与大小写（Windows 路径不区分大小写） */
function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  return norm(a) === norm(b);
}

/** 读出拖入文件夹的第一层子项名称（浏览器定位同名文件夹时用来消歧）；readEntries 每次最多回 100 项 */
async function readChildNames(entry: FileSystemDirectoryEntry): Promise<string[]> {
  const reader = entry.createReader();
  const names: string[] = [];
  for (let i = 0; i < 3; i++) {
    const batch = await new Promise<FileSystemEntry[]>((resolve) => reader.readEntries(resolve, () => resolve([])));
    if (batch.length === 0) break;
    names.push(...batch.map((e) => e.name));
  }
  return names;
}

export function ProjectDialog() {
  const open = useProjectDialogStore((s) => s.open);
  const mode = useProjectDialogStore((s) => s.mode);
  const projectId = useProjectDialogStore((s) => s.projectId);
  const hide = useProjectDialogStore((s) => s.hide);
  const projects = useChatStore((s) => s.projects);
  const editing = mode === "edit" ? projects.find((p) => p.id === projectId) ?? null : null;

  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [icon, setIcon] = useState(DEFAULT_PROJECT_ICON);
  const [color, setColor] = useState("blue");
  const [index, setIndex] = useState(true);
  const [indexInitial, setIndexInitial] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [over, setOver] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [candidates, setCandidates] = useState<Array<{ name: string; path: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** 名称是否由文件夹自动带出（用户手动改过名称后，换文件夹不再覆盖名称） */
  const autoNameRef = useRef(true);
  const nameRef = useRef<HTMLInputElement>(null);
  /** dragenter / dragleave 会在子元素间反复触发：用深度计数避免投放区高亮闪烁 */
  const dragDepth = useRef(0);
  const canPickNative = typeof window !== "undefined" && Boolean(window.chatcoderAPI?.selectDirectory);
  const canDrop = mode === "create";

  // 每次打开按模式初始化（关闭期间保留上一次内容，避免退场动画中内容跳变）
  useEffect(() => {
    if (!open) return;
    setError(null);
    setNotice(null);
    setBusy(false);
    setBrowsing(false);
    setOver(false);
    setResolving(false);
    setCandidates([]);
    dragDepth.current = 0;
    if (mode === "edit" && editing) {
      setName(editing.name || baseName(editing.path));
      setPath(editing.path);
      setIcon(projectIconKey(editing.icon));
      setColor(projectColorKey(editing.color));
      autoNameRef.current = false;
      setIndexInitial(null);
      void api.symbolIndexWorkspaces()
        .then((res) => {
          const hit = res.workspaces.find((w) => samePath(w.workspace, editing.path));
          const on = Boolean(hit?.enabled);
          setIndex(on);
          setIndexInitial(on);
        })
        .catch(() => { setIndex(false); setIndexInitial(false); });
      window.setTimeout(() => nameRef.current?.focus(), 60);
    } else {
      setName("");
      setPath("");
      setIcon(DEFAULT_PROJECT_ICON);
      setColor(suggestProjectColor(useChatStore.getState().projects.map((p) => p.color)));
      setIndex(true);
      setIndexInitial(null);
      autoNameRef.current = true;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, projectId]);

  const applyPath = (dir: string) => {
    const clean = dir.trim();
    if (!clean) return;
    setPath(clean);
    setError(null);
    setBrowsing(false);
    setCandidates([]);
    if (autoNameRef.current || !name.trim()) {
      setName(baseName(clean));
      autoNameRef.current = true;
    }
  };

  /** 点击选择：永远是系统自带的文件夹选择窗口——桌面版由 Electron 主进程弹出；
   *  浏览器里页面自己弹不出带绝对路径的选择器，于是请本机后端代为弹出。
   *  只有后端也弹不出时（后端不在本机 / 无桌面会话）才回落到自绘的目录浏览器。 */
  const pickFolder = async () => {
    if (picking || resolving) return;
    setNotice(null);
    setError(null);
    setPicking(true);
    try {
      if (canPickNative) {
        const dir = await window.chatcoderAPI?.selectDirectory?.();
        if (dir) applyPath(dir);
        return;
      }
      const res = await api.pickDirectory();
      if (res.path) applyPath(res.path);
      else if (res.unsupported) setBrowsing(true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError(e.message);
      else setBrowsing(true); // 旧版后端没有该接口 / 请求失败：给用户一个能继续的办法
    } finally {
      setPicking(false);
    }
  };

  /** 处理拖入：必须在 drop 事件内同步取出 entry / File（事件结束后 DataTransfer 会被清空），再做异步定位 */
  const handleDrop = (e: DragEvent<HTMLElement>) => {
    e.preventDefault();
    dragDepth.current = 0;
    setOver(false);
    if (!canDrop || busy) return;
    const picked = Array.from(e.dataTransfer.items ?? [])
      .filter((it) => it.kind === "file")
      .map((it) => ({ entry: it.webkitGetAsEntry?.() ?? null, file: it.getAsFile() }));
    const dirs = picked.filter((p) => p.entry?.isDirectory);
    setError(null);
    setNotice(null);
    setCandidates([]);
    if (dirs.length === 0) {
      setError(picked.length > 0 ? "拖入的是文件，请拖入文件夹" : "没有识别到文件夹，请换个方式试试");
      return;
    }
    const first = dirs[0];
    if (dirs.length > 1) setNotice(`一个项目只对应一个文件夹，已使用「${first.entry?.name ?? ""}」`);

    // 桌面版：preload 直接给出绝对路径
    const abs = first.file ? window.chatcoderAPI?.getPathForFile?.(first.file) : "";
    if (abs) { applyPath(abs); return; }

    // 浏览器：读不到路径，按「名称 + 子项」让后端在本机定位
    const entry = first.entry as FileSystemDirectoryEntry;
    setResolving(true);
    void (async () => {
      try {
        const kids = await readChildNames(entry);
        const res = await api.resolveDir(entry.name, kids);
        if (res.candidates.length === 1) applyPath(res.candidates[0].path);
        else if (res.candidates.length > 1) setCandidates(res.candidates);
        else setError(`没能在本机找到「${entry.name}」，请点击这里选择它`);
      } catch {
        setError("定位文件夹失败，请点击这里选择它");
      } finally {
        setResolving(false);
      }
    })();
  };

  const isFileDrag = (e: DragEvent<HTMLElement>) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
  const dropProps = canDrop
    ? {
        onDragEnter: (e: DragEvent<HTMLElement>) => { if (!isFileDrag(e)) return; e.preventDefault(); dragDepth.current += 1; setOver(true); },
        onDragOver: (e: DragEvent<HTMLElement>) => { if (!isFileDrag(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; },
        onDragLeave: (e: DragEvent<HTMLElement>) => { if (!isFileDrag(e)) return; dragDepth.current = Math.max(0, dragDepth.current - 1); if (dragDepth.current === 0) setOver(false); },
        onDrop: handleDrop,
      }
    : {};

  const submit = async () => {
    if (busy) return;
    const finalName = name.trim();
    if (mode === "edit") {
      if (!editing) return;
      setBusy(true);
      const ok = await useChatStore.getState().updateProjectLook(editing.id, {
        name: finalName || baseName(editing.path),
        icon,
        color,
      });
      if (ok && indexInitial !== null && index !== indexInitial) {
        await (index ? api.symbolIndexEnable(editing.path) : api.symbolIndexDisable(editing.path)).catch(() => {
          useChatStore.setState({ error: "索引设置未能保存，可在「设置 → 索引库」中重试" });
        });
      }
      setBusy(false);
      if (ok) hide();
      return;
    }
    if (!path) {
      setError("请先添加源文件夹");
      return;
    }
    setBusy(true);
    setError(null);
    const project = await useChatStore.getState().createProject(path, finalName || undefined, { icon, color });
    if (!project) {
      setBusy(false);
      // 同路径项目已归档：由「恢复并打开」提示接管，本弹窗让位
      if (useChatStore.getState().archivedProjectPrompt) { hide(); return; }
      const msg = useChatStore.getState().error;
      setError(msg ? msg.replace(/^Error:\s*/, "") : "创建失败，请检查文件夹是否存在且可读写");
      useChatStore.setState({ error: null });
      return;
    }
    if (index) {
      void api.symbolIndexEnable(project.path).catch(() => {
        useChatStore.setState({ error: "项目已创建，但索引未能开启，可在「设置 → 索引库」中重试" });
      });
    }
    setBusy(false);
    hide();
  };

  const colorLabel = useMemo(() => PROJECT_COLORS.find((c) => c.key === color)?.label ?? "", [color]);
  const iconLabel = useMemo(() => PROJECT_ICON_DEFS.find((d) => d.key === icon)?.label ?? "", [icon]);
  const title = mode === "edit" ? "编辑项目" : "新建项目";

  return (
    <Dialog
      open={open}
      onClose={hide}
      width={600}
      title={title}
      className="pd-dialog"
      footer={
        <>
          <Button variant="secondary" onClick={hide}>取消</Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy} disabled={mode === "create" && !path}>
            {mode === "edit" ? "保存" : "创建项目"}
          </Button>
        </>
      }
    >
      {/* 整个正文都是投放区：文件夹拖到弹窗里的任何位置都能接住，不必瞄准虚线框 */}
      <div className={"pd-root" + (over ? " is-over" : "")} {...dropProps}>
        <DialogSection title="源文件夹" className="pd-sec">
          {browsing ? (
            <FolderBrowser onPick={applyPath} onCancel={() => setBrowsing(false)} />
          ) : path ? (
            <InsetCard>
              <div className="pd-folder">
                <FileIcon name={path} folder size={20} />
                <span className="pd-folder-main">
                  <span className="pd-folder-name">{baseName(path)}</span>
                  <Tooltip content={path} side="bottom"><span className="pd-folder-path">{path}</span></Tooltip>
                </span>
                {mode === "create" && (
                  <span className="pd-folder-ops">
                    <Button size="sm" variant="ghost" icon={<IconRefresh size={13} />} onClick={() => void pickFolder()}>更换</Button>
                    <Tooltip content="移除文件夹" side="top">
                      <button type="button" className="ui-icon-btn" aria-label="移除文件夹" onClick={() => { setPath(""); if (autoNameRef.current) setName(""); }}>
                        <IconX size={14} />
                      </button>
                    </Tooltip>
                  </span>
                )}
              </div>
              {mode === "edit" && <div className="pd-folder-note">项目创建后不可更换源文件夹</div>}
              {mode === "create" && over && <div className="pd-folder-over">松开即可换成这个文件夹</div>}
            </InsetCard>
          ) : (
            <div className={"pd-drop-frame" + (over ? " is-over" : "")}>
              <button
                type="button"
                className={"pd-drop" + (over ? " is-over" : "") + (error && !candidates.length ? " is-invalid" : "")}
                onClick={() => void pickFolder()}
                disabled={picking || resolving}
              >
                <span className="pd-drop-ic"><IconFolderPlus size={24} /></span>
                <span className="pd-drop-title">
                  {over ? "松开，添加这个文件夹" : resolving ? "正在定位文件夹…" : picking ? "请在弹出的窗口中选择文件夹…" : "拖入文件夹，或点击选择"}
                </span>
                <span className="pd-drop-sub">智能体可读写该文件夹，会话、文件与索引都以它为根目录</span>
              </button>
            </div>
          )}
          {candidates.length > 0 && (
            <div className="pd-cands" role="group" aria-label="找到多个同名文件夹">
              <div className="pd-cands-head">本机有多个同名文件夹，请选择你拖入的那个</div>
              {candidates.map((c) => (
                <button key={c.path} type="button" className="pd-cand" onClick={() => applyPath(c.path)}>
                  <FileIcon name={c.name} folder size={16} />
                  <span className="pd-cand-path">{c.path}</span>
                </button>
              ))}
            </div>
          )}
          {notice && <div className="pd-notice">{notice}</div>}
          {error && <div className="pd-error" role="alert">{error}</div>}
        </DialogSection>

        <DialogSection title="项目名称" className="pd-sec">
          {/* 左侧为图标实时预览，右侧输入名称（参考图1）：选图标 / 颜色时这里同步变化 */}
          <InsetCard className="pd-name-card">
            <div className="pd-name">
              <Tooltip content={`${iconLabel} · ${colorLabel}`} side="top">
                <span className="pd-name-preview">
                  <ProjectIcon icon={icon} color={color} size={22} tile tileSize={40} />
                </span>
              </Tooltip>
              <input
                ref={nameRef}
                className="pd-name-input"
                value={name}
                placeholder="输入名称…"
                maxLength={80}
                onChange={(e) => { setName(e.target.value); autoNameRef.current = e.target.value.trim() === ""; }}
                onKeyDown={(e) => {
                  const native = e.nativeEvent as KeyboardEvent & { isComposing?: boolean };
                  if (e.key === "Enter" && !native.isComposing && e.keyCode !== 229) { e.preventDefault(); void submit(); }
                }}
              />
            </div>
          </InsetCard>
        </DialogSection>

        <DialogSection title="项目图标" className="pd-sec">
          <div className="pd-icons" role="radiogroup" aria-label="项目图标" style={{ ["--pick-c" as string]: `var(--pc-${color})` }}>
            {PROJECT_ICON_DEFS.map((d) => (
              <Tooltip key={d.key} content={d.label} side="top">
                <button
                  type="button"
                  role="radio"
                  aria-checked={icon === d.key}
                  aria-label={d.label}
                  className={"pd-icon-cell" + (icon === d.key ? " is-active" : "")}
                  onClick={() => setIcon(d.key)}
                >
                  <ProjectIcon icon={d.key} color={color} size={18} />
                </button>
              </Tooltip>
            ))}
          </div>
        </DialogSection>

        <DialogSection title="图标颜色" aside={<span>{colorLabel}</span>} className="pd-sec">
          <div className="pd-colors" role="radiogroup" aria-label="图标颜色">
            {PROJECT_COLORS.map((c) => (
              <Tooltip key={c.key} content={c.label} side="top">
                <button
                  type="button"
                  role="radio"
                  aria-checked={color === c.key}
                  aria-label={c.label}
                  className={"pd-color" + (color === c.key ? " is-active" : "")}
                  style={{ ["--swatch" as string]: `var(--pc-${c.key})` }}
                  onClick={() => setColor(c.key)}
                />
              </Tooltip>
            ))}
          </div>
        </DialogSection>

        <div className="pd-divider" />

        <div className="pd-index">
          <div className="pd-index-text">
            <div className="pd-index-title">项目索引</div>
            <div className="pd-index-desc">
              建立代码索引可增强上下文理解能力，让智能体更快找到相关代码、回答更准确。索引在本机生成，不会上传。
            </div>
          </div>
          <Switch checked={index} onChange={setIndex} disabled={mode === "edit" && indexInitial === null} aria-label="建立项目索引" />
        </div>
      </div>
    </Dialog>
  );
}
