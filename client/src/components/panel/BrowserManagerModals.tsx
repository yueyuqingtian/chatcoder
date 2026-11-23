import { useEffect, useRef, useState } from "react";
import { api, type BrowserBookmarkOut, type BrowserHistoryOut } from "../../api/client";
import { ConfirmDialog } from "../ConfirmDialog";
import {
  IconCopy,
  IconDownload,
  IconFolder,
  IconGlobe,
  IconHistory,
  IconLock,
  IconPencil,
  IconPin,
  IconPlus,
  IconSearch,
  IconStar,
  IconTrash,
} from "../icons";
import { Button, Collapse, Dialog, IconButton, Input, Segmented } from "../ui";
import { isBrowserHttpUrl, useBrowserLibrary } from "./browserLibrary";

export type BrowserManager = "bookmarks" | "history" | "vault" | "downloads";
export type VaultItem = NonNullable<Awaited<ReturnType<NonNullable<NonNullable<Window["chatcoderAPI"]>["browserVaultList"]>>>["items"]>[number];
export interface BrowserDownload {
  id: number;
  filename: string;
  path: string;
  state: string;
  received: number;
  total: number;
}

const managerTitles: Record<BrowserManager, string> = {
  bookmarks: "书签管理",
  history: "浏览历史",
  vault: "密码库",
  downloads: "下载管理",
};

const managerSubtitles: Record<BrowserManager, string> = {
  bookmarks: "收藏与置顶常用开发站点、接口文档",
  history: "回溯近期在内置浏览器访问过的页面",
  vault: "由本机系统钥匙串加密保存的网站登录凭据",
  downloads: "查看文件下载状态并快速打开所在文件",
};

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const download = document.createElement("a");
  download.href = url;
  download.download = filename;
  download.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function BrowserManagerModals({
  mode,
  onMode,
  url,
  title,
  activeBookmark,
  bookmarkToEdit,
  downloads,
  isElectron = true,
  onNavigate,
  onVaultChanged,
}: {
  mode: BrowserManager | null;
  onMode: (mode: BrowserManager | null) => void;
  url: string;
  title: string;
  activeBookmark?: BrowserBookmarkOut;
  bookmarkToEdit: BrowserBookmarkOut | null;
  downloads: BrowserDownload[];
  isElectron?: boolean;
  onNavigate: (url: string) => void;
  onVaultChanged: () => void;
}) {
  const bookmarks = useBrowserLibrary((state) => state.bookmarks);
  const refreshBookmarks = useBrowserLibrary((state) => state.refreshBookmarks);
  const refreshHistory = useBrowserLibrary((state) => state.refreshHistory);
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState<BrowserHistoryOut[]>([]);
  const [vault, setVault] = useState<VaultItem[]>([]);
  const [neverOrigins, setNeverOrigins] = useState<string[]>([]);
  const [vaultAvailable, setVaultAvailable] = useState(false);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [reload, setReload] = useState(0);
  const [folder, setFolder] = useState("");
  const [bookmarkEdit, setBookmarkEdit] = useState<{ id?: number; title: string; url: string; folder: string } | null>(null);
  const [vaultEdit, setVaultEdit] = useState<{ id?: string; url: string; username: string; password: string; label: string } | null>(null);
  const [confirm, setConfirm] = useState<{ title: string; message: string; label: string; action: () => Promise<void>; danger?: boolean } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setQuery(""); setFolder(""); setError(""); setStatus("");
    setBookmarkEdit(mode === "bookmarks" ? bookmarkToEdit : null); setVaultEdit(null); setConfirm(null);
  }, [mode, bookmarkToEdit]);

  useEffect(() => {
    if (mode !== "history" && mode !== "vault") return;
    let active = true;
    setLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        if (mode === "history") {
          const response = await api.listBrowserHistory(query, 250);
          if (active) setHistory(response.items);
        } else {
          const response = await window.chatcoderAPI?.browserVaultList?.();
          if (!response?.ok) throw new Error(response?.error || "此环境无法访问本机密码库");
          if (active) {
            setVault(response.items ?? []);
            setNeverOrigins(response.neverOrigins ?? []);
            setVaultAvailable(response.encryptedAvailable === true);
          }
        }
      } catch (reason) { if (active) setError(String(reason)); }
      finally { if (active) setLoading(false); }
    }, mode === "history" && query ? 220 : 0);
    return () => { active = false; window.clearTimeout(timer); };
  }, [mode, query, reload]);

  const operate = async (action: () => Promise<void>) => {
    setWorking(true); setError(""); setStatus("");
    try { await action(); return true; }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); return false; }
    finally { setWorking(false); }
  };
  const visit = (target: string) => { onMode(null); onNavigate(target); };
  const keyword = query.trim().toLowerCase();
  const visibleBookmarks = bookmarks.filter((item) =>
    (!folder || item.folder === folder) && [item.title, item.url, item.folder].some((text) => text.toLowerCase().includes(keyword)));
  const visibleVault = vault.filter((item) => [item.origin, item.username, item.label ?? ""].some((text) => text.toLowerCase().includes(keyword)));
  const folders = Array.from(new Set(bookmarks.map((item) => item.folder).filter(Boolean)));
  const saveBookmark = () => {
    if (!bookmarkEdit || !isBrowserHttpUrl(bookmarkEdit.url.trim())) return;
    void operate(async () => {
      if (bookmarkEdit.id) await api.updateBrowserBookmark(bookmarkEdit.id, bookmarkEdit);
      else await api.saveBrowserBookmark(bookmarkEdit);
      await refreshBookmarks();
      setBookmarkEdit(null);
    });
  };
  const pinBookmark = (item: BrowserBookmarkOut) => void operate(async () => {
    const first = bookmarks.reduce((order, bookmark) => Math.min(order, bookmark.sort_order), 0);
    const sortOrder = item.sort_order < 0 ? 0 : Math.max(-2_147_483_648, first - 1);
    await api.updateBrowserBookmark(item.id, { sort_order: sortOrder });
    await refreshBookmarks();
  });
  const saveVault = () => {
    if (!vaultEdit || !vaultEdit.username.trim() || !isBrowserHttpUrl(vaultEdit.url.trim()) || (!vaultEdit.id && !vaultEdit.password)) return;
    void operate(async () => {
      const result = await window.chatcoderAPI?.browserVaultSave?.({ ...vaultEdit, password: vaultEdit.password || undefined });
      if (!result?.ok) throw new Error(result?.error || "密码保存失败");
      setVaultEdit(null); setReload((value) => value + 1); onVaultChanged();
    });
  };
  const generatePassword = () => {
    if (!vaultEdit) return;
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    const value = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_");
    setVaultEdit({ ...vaultEdit, password: value });
    setStatus("已生成高强度随机密码，保存后可在密码库复制");
  };
  const clearHistory = (days?: number) => setConfirm({
    title: days ? "清理最近一天的历史" : "清空全部浏览历史",
    message: days ? "删除最近 24 小时的浏览记录？" : "确认删除所有会话共享的浏览记录？此操作不可撤销。",
    label: "确认清理", danger: true,
    action: async () => { await api.clearBrowserHistory(days); await refreshHistory(); setReload((value) => value + 1); },
  });

  const activeMode = mode ?? "bookmarks";
  const tabOptions: Array<{ value: BrowserManager; label: string; count?: number; disabled?: boolean }> = [
    { value: "bookmarks", label: "书签" },
    { value: "history", label: "历史" },
    { value: "vault", label: "密码库", disabled: !isElectron },
    { value: "downloads", label: "下载", count: downloads.length > 0 ? downloads.length : undefined, disabled: !isElectron },
  ];

  const headerActions = mode === "downloads" ? (
    <Button
      size="sm"
      variant="secondary"
      icon={<IconFolder size={14} />}
      onClick={() => void operate(async () => {
        const result = await window.chatcoderAPI?.browserAgentExec?.(0, "downloadDir", { action: "open" });
        if (!result?.ok) throw new Error(result?.error || "无法打开下载目录");
      })}
    >
      打开下载目录
    </Button>
  ) : mode === "bookmarks" ? (
    <Button
      size="sm"
      variant="primary"
      icon={<IconPlus size={14} />}
      disabled={working}
      onClick={() => setBookmarkEdit(activeBookmark ?? { title, url: isBrowserHttpUrl(url) ? url : "", folder: "" })}
    >
      {activeBookmark ? "编辑当前页" : "新建书签"}
    </Button>
  ) : mode === "vault" ? (
    <Button
      size="sm"
      variant="primary"
      icon={<IconPlus size={14} />}
      disabled={!vaultAvailable || working}
      onClick={() => setVaultEdit({ url: isBrowserHttpUrl(url) ? new URL(url).origin : "", username: "", password: "", label: "" })}
    >
      添加账号
    </Button>
  ) : undefined;

  return <>
    <Dialog
      open={mode != null}
      title={mode ? managerTitles[mode] : "浏览器管理"}
      subtitle={mode ? managerSubtitles[mode] : undefined}
      width={660}
      className="bp-manager-dialog"
      bodyClassName="bp-manager-body"
      actions={headerActions}
      onClose={() => { if (!working) onMode(null); }}
    >
      <div className="bp-manager">
        <div className="bp-manager-nav">
          <Segmented
            size="sm"
            value={activeMode}
            onChange={(val) => onMode(val as BrowserManager)}
            aria-label="管理模块切换"
            options={tabOptions}
          />
          {mode === "bookmarks" && (
            <div className="bp-manager-nav-end">
              <Button size="xs" variant="ghost" disabled={working} onClick={() => fileInput.current?.click()}>导入 JSON</Button>
              <Button size="xs" variant="ghost" disabled={working || bookmarks.length === 0} onClick={() => void operate(async () => {
                const data = await api.exportBrowserBookmarks();
                downloadBlob(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), "chatcoder-bookmarks.json");
              })}>导出</Button>
            </div>
          )}
          {mode === "history" && (
            <div className="bp-manager-nav-end">
              <Button size="xs" variant="ghost" disabled={working || history.length === 0} onClick={() => clearHistory(1)}>清理 24 小时</Button>
              <Button size="xs" variant="danger-ghost" disabled={working || history.length === 0} onClick={() => clearHistory()}>清空历史</Button>
            </div>
          )}
        </div>

        {(mode === "bookmarks" || mode === "history" || mode === "vault") && (
          <div className="bp-manager-search-row">
            <Input
              prefix={<IconSearch size={14} />}
              clearable
              size="sm"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="搜索记录"
              placeholder={
                mode === "bookmarks" ? "搜索书签名称、网址或文件夹…"
                  : mode === "history" ? "搜索浏览标题或网址…"
                    : "搜索站点、账号或备注…"
              }
            />
          </div>
        )}

        <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (file) void operate(async () => {
            if (file.size > 5_000_000) throw new Error("书签文件超过 5 MB");
            const data = JSON.parse(await file.text()) as { items?: unknown[] };
            if (!Array.isArray(data?.items)) throw new Error("请选择从 ChatCoder 导出的书签文件");
            const result = await api.importBrowserBookmarks(data.items);
            await refreshBookmarks();
            setStatus(`已导入 ${result.imported} 条书签`);
          });
        }} />

        {error && <div className="resource-notice" role="alert">{error}</div>}
        {status && <div className="bp-manager-status" role="status">{status}</div>}

        {mode === "bookmarks" && <>
          {folders.length > 0 && (
            <div className="bp-manager-folders">
              <Segmented
                size="sm"
                value={folder}
                aria-label="书签文件夹"
                onChange={setFolder}
                options={[{ value: "", label: "全部" }, ...folders.map((name) => ({ value: name, label: name }))]}
              />
            </div>
          )}
          <Collapse open={bookmarkEdit != null} lazy>
            {bookmarkEdit && (
              <form className="bp-manager-form" onSubmit={(event) => { event.preventDefault(); saveBookmark(); }}>
                <div className="bp-manager-form-title">{bookmarkEdit.id ? "编辑书签" : "新建书签"}</div>
                <Input aria-label="书签名称" placeholder="书签名称" maxLength={300} value={bookmarkEdit.title}
                  onChange={(event) => setBookmarkEdit({ ...bookmarkEdit, title: event.target.value })} />
                <Input aria-label="书签网址" placeholder="https://example.com" maxLength={2000} value={bookmarkEdit.url}
                  onChange={(event) => setBookmarkEdit({ ...bookmarkEdit, url: event.target.value })} />
                <Input aria-label="书签文件夹" placeholder="文件夹（选填）" maxLength={120} value={bookmarkEdit.folder}
                  onChange={(event) => setBookmarkEdit({ ...bookmarkEdit, folder: event.target.value })} />
                <div className="bp-manager-form-actions">
                  <Button size="sm" variant="ghost" onClick={() => setBookmarkEdit(null)}>取消</Button>
                  <Button size="sm" variant="primary" type="submit" disabled={working || !isBrowserHttpUrl(bookmarkEdit.url.trim())}>保存</Button>
                </div>
              </form>
            )}
          </Collapse>
          <div className="bp-manager-card">
            {visibleBookmarks.length === 0 ? (
              <div className="bp-manager-empty">
                <span className="bp-manager-empty-ico"><IconStar size={18} /></span>
                <div className="bp-manager-empty-title">{query ? "没有匹配的书签" : "暂无书签"}</div>
                <div className="bp-manager-empty-desc">{query ? "尝试更换搜索关键词或清除文件夹筛选" : "点击右上角「新建书签」或地址栏星标快速收藏网页"}</div>
              </div>
            ) : (
              <div className="bp-manager-list">
                {visibleBookmarks.map((item) => (
                  <div className="bp-manager-row" key={item.id}>
                    <button type="button" className="bp-manager-hit" onClick={() => visit(item.url)}>
                      <span className={`bp-manager-ico${item.sort_order < 0 ? " is-accent" : ""}`}>
                        <IconStar size={14} />
                      </span>
                      <span className="bp-manager-entry">
                        <span className="bp-manager-title-line">
                          <strong>{item.title || item.url}</strong>
                          {item.folder && <span className="bp-manager-tag">{item.folder}</span>}
                          {item.sort_order < 0 && <span className="bp-manager-tag is-pin">置顶</span>}
                        </span>
                        <small>{item.url}</small>
                      </span>
                    </button>
                    <div className="bp-manager-row-actions">
                      <IconButton size="xs" title={item.sort_order < 0 ? "取消置顶" : "置顶"} active={item.sort_order < 0}
                        icon={<IconPin size={13} />} disabled={working} onClick={() => pinBookmark(item)} />
                      <IconButton size="xs" title="编辑书签" icon={<IconPencil size={13} />} disabled={working}
                        onClick={() => setBookmarkEdit(item)} />
                      <IconButton size="xs" title="删除书签" tone="danger" icon={<IconTrash size={13} />} disabled={working}
                        onClick={() => void operate(async () => { await api.deleteBrowserBookmark(item.id); await refreshBookmarks(); })} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>}

        {mode === "history" && <>
          <div className="bp-manager-card">
            {loading ? (
              <div className="bp-manager-empty">
                <div className="bp-manager-empty-desc">正在读取浏览历史…</div>
              </div>
            ) : !history.length ? (
              <div className="bp-manager-empty">
                <span className="bp-manager-empty-ico"><IconHistory size={18} /></span>
                <div className="bp-manager-empty-title">{query ? "没有匹配的记录" : "暂无浏览历史"}</div>
                <div className="bp-manager-empty-desc">{query ? "尝试缩短或更换搜索关键词" : "在内置浏览器中打开网页后，访问记录会保留在这里"}</div>
              </div>
            ) : (
              <div className="bp-manager-list">
                {history.map((item) => (
                  <div className="bp-manager-row" key={item.id}>
                    <button type="button" className="bp-manager-hit" onClick={() => visit(item.url)}>
                      <span className="bp-manager-ico"><IconGlobe size={14} /></span>
                      <span className="bp-manager-entry">
                        <strong>{item.title || item.url}</strong>
                        <small>{item.url}</small>
                      </span>
                    </button>
                    <div className="bp-manager-row-actions">
                      <time className="bp-manager-time" dateTime={item.last_visited_at}>
                        {new Date(item.last_visited_at).toLocaleDateString("zh-CN")}
                      </time>
                      <IconButton size="xs" title="删除这条记录" tone="danger" disabled={working} icon={<IconTrash size={13} />}
                        onClick={() => void operate(async () => { await api.deleteBrowserHistoryItem(item.id); await refreshHistory(); setReload((value) => value + 1); })} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>}

        {mode === "vault" && <>
          {!vaultAvailable && !loading && <div className="resource-notice" role="status">当前系统加密服务不可用，无法读写密码库</div>}
          <Collapse open={vaultEdit != null} lazy>
            {vaultEdit && (
              <form className="bp-manager-form" onSubmit={(event) => { event.preventDefault(); saveVault(); }}>
                <div className="bp-manager-form-title">{vaultEdit.id ? "更新账号凭据" : "添加账号凭据"}</div>
                <Input value={vaultEdit.url} aria-label="登录网站" placeholder="网站地址（如 https://github.com）" disabled={!!vaultEdit.id}
                  onChange={(event) => setVaultEdit({ ...vaultEdit, url: event.target.value })} />
                <Input value={vaultEdit.username} aria-label="账号" placeholder="用户名或邮箱" autoComplete="off"
                  onChange={(event) => setVaultEdit({ ...vaultEdit, username: event.target.value })} />
                <Input value={vaultEdit.password} aria-label="密码" placeholder={vaultEdit.id ? "输入新密码以更新（留空不修改）" : "密码"}
                  type="password" autoComplete="new-password"
                  onChange={(event) => setVaultEdit({ ...vaultEdit, password: event.target.value })} />
                <Input value={vaultEdit.label} aria-label="备注" placeholder="备注（选填）"
                  onChange={(event) => setVaultEdit({ ...vaultEdit, label: event.target.value })} />
                <div className="bp-manager-form-actions">
                  <Button size="sm" variant="ghost" disabled={working} onClick={generatePassword}>生成随机密码</Button>
                  <Button size="sm" variant="ghost" onClick={() => setVaultEdit(null)}>取消</Button>
                  <Button size="sm" variant="primary" type="submit"
                    disabled={working || !isBrowserHttpUrl(vaultEdit.url.trim()) || !vaultEdit.username.trim() || (!vaultEdit.id && !vaultEdit.password)}>保存</Button>
                </div>
              </form>
            )}
          </Collapse>
          <div className="bp-manager-card">
            {!loading && visibleVault.length === 0 ? (
              <div className="bp-manager-empty">
                <span className="bp-manager-empty-ico"><IconLock size={18} /></span>
                <div className="bp-manager-empty-title">{query ? "没有匹配的账号" : "暂无已保存账号"}</div>
                <div className="bp-manager-empty-desc">{query ? "尝试更换搜索条件" : "在内置浏览器登录网页或点击右上角「添加账号」即可安全保存"}</div>
              </div>
            ) : (
              <div className="bp-manager-list">
                {visibleVault.map((item) => (
                  <div className="bp-manager-row" key={item.id}>
                    <span className="bp-manager-ico"><IconLock size={14} /></span>
                    <span className="bp-manager-entry">
                      <span className="bp-manager-title-line">
                        <strong>{item.username}</strong>
                        {item.label && <span className="bp-manager-tag">{item.label}</span>}
                      </span>
                      <small>{item.origin}</small>
                    </span>
                    <div className="bp-manager-row-actions">
                      <IconButton size="xs" title="编辑账号" icon={<IconPencil size={13} />} disabled={working}
                        onClick={() => setVaultEdit({ ...item, password: "", label: item.label ?? "" })} />
                      <IconButton size="xs" title="复制密码" icon={<IconCopy size={13} />} disabled={working || !vaultAvailable} onClick={() => setConfirm({
                        title: "复制密码到系统剪贴板", message: `确认复制 ${item.origin} 的账号「${item.username}」密码？`, label: "复制密码",
                        action: async () => {
                          const result = await window.chatcoderAPI?.browserVaultCopyPassword?.(item.id);
                          if (!result?.ok) throw new Error(result?.error || "复制失败");
                          setStatus("已复制密码到剪贴板");
                        },
                      })} />
                      <IconButton size="xs" title="删除账号" tone="danger" icon={<IconTrash size={13} />} disabled={working} onClick={() => setConfirm({
                        title: "删除保存的账号", message: `确定从此设备永久删除「${item.username}」的登录凭据？`, label: "删除", danger: true,
                        action: async () => {
                          const result = await window.chatcoderAPI?.browserVaultDelete?.(item.id);
                          if (!result?.ok) throw new Error(result?.error || "删除失败");
                          setReload((value) => value + 1); onVaultChanged();
                        },
                      })} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
          {neverOrigins.length > 0 && (
            <div className="bp-manager-card">
              <div className="bp-manager-subhead">已关闭保存提醒的站点</div>
              <div className="bp-manager-list">
                {neverOrigins.map((origin) => (
                  <div className="bp-manager-row" key={origin}>
                    <span className="bp-manager-ico"><IconGlobe size={14} /></span>
                    <span className="bp-manager-entry"><strong>{origin}</strong><small>登录时不再提示保存密码</small></span>
                    <Button size="xs" variant="secondary" disabled={working} onClick={() => void operate(async () => {
                      const result = await window.chatcoderAPI?.browserVaultSetNever?.(origin, false);
                      if (!result?.ok) throw new Error(result?.error || "无法恢复提醒");
                      setReload((value) => value + 1);
                    })}>恢复提醒</Button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>}

        {mode === "downloads" && (
          <div className="bp-manager-card">
            {!downloads.length ? (
              <div className="bp-manager-empty">
                <span className="bp-manager-empty-ico"><IconDownload size={18} /></span>
                <div className="bp-manager-empty-title">暂无下载记录</div>
                <div className="bp-manager-empty-desc">通过内置浏览器下载的文件会展示在这里，可实时查看进度并快速打开</div>
              </div>
            ) : (
              <div className="bp-manager-list">
                {[...downloads].reverse().map((item) => {
                  const pct = item.total > 0 ? Math.min(100, Math.round((item.received / item.total) * 100)) : 0;
                  const stateLabel = item.state === "completed" ? "已完成"
                    : item.state === "progressing" ? (item.total > 0 ? `下载中 ${pct}%` : "下载中…")
                      : item.state === "cancelled" ? "已取消" : "下载中断";
                  const sizeInfo = [formatBytes(item.received), item.total > 0 ? formatBytes(item.total) : ""].filter(Boolean).join(" / ");
                  return (
                    <div className="bp-manager-row" key={item.id}>
                      <span className={`bp-manager-ico${item.state === "completed" ? " is-ok" : ""}`}>
                        <IconDownload size={14} />
                      </span>
                      <span className="bp-manager-entry">
                        <span className="bp-manager-title-line">
                          <strong>{item.filename}</strong>
                          <span className={`bp-manager-tag${item.state === "completed" ? " is-ok" : item.state === "progressing" ? " is-accent" : " is-muted"}`}>
                            {stateLabel}
                          </span>
                        </span>
                        <small>{item.path || sizeInfo || "正在写入本地…"}</small>
                        {item.state === "progressing" && item.total > 0 && (
                          <span className="bp-dl-track" aria-hidden="true">
                            <span className="bp-dl-bar" style={{ width: `${pct}%` }} />
                          </span>
                        )}
                      </span>
                      <div className="bp-manager-row-actions">
                        <Button
                          size="xs"
                          variant="secondary"
                          disabled={item.state !== "completed"}
                          onClick={() => void window.chatcoderAPI?.openPath?.(item.path)}
                        >
                          打开文件
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </Dialog>
    <ConfirmDialog
      open={confirm != null}
      title={confirm?.title ?? ""}
      message={confirm?.message ?? ""}
      danger={confirm?.danger}
      confirmLabel={confirm?.label}
      busy={working}
      busyLabel="处理中…"
      onCancel={() => setConfirm(null)}
      onConfirm={() => { if (confirm) void operate(confirm.action).then(() => setConfirm(null)); }}
    />
  </>;
}
