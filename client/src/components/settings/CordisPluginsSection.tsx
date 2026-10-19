/** dsh 第三方插件（plan-20261017-goal12 项 12，范围：只兼容第三方插件协议）。
 *
 * 安装：选择插件目录或 `npm pack` 生成的 .tgz，主进程校验清单（name / version / dsh.bundle.patch）后安装。
 * 启停：停用的插件保留安装目录，重新启用时重新加载；卸载会删除安装目录，先确认。
 * 插件集合变化时主进程广播 `cordis:changed`，本页订阅后即时刷新（无需重开页面）。
 * 含 dsh 客户端模块（dsh.client）的插件只运行宿主半，界面上会明确标注。 */
import { useEffect, useMemo, useState } from "react";
import { Button, Dialog, Switch, Tooltip } from "../ui";
import { IconBox, IconInfo, IconRefresh, IconTrash } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";

interface CordisStatus {
  id: string;
  dir: string;
  name: string;
  version: string | null;
  description: string;
  status: "loading" | "loaded" | "failed" | "disabled";
  error: string | null;
  /** 加载成功但有提示（如缺少依赖的服务），不影响其它功能 */
  warnings?: string[];
  /** 是否携带 dsh 客户端模块（含界面扩展；本软件只运行宿主半） */
  hasClient?: boolean;
  clientPlatform?: string | null;
}
interface CordisNote { plugin: string; level: string; message: string; at: number }
interface CordisResult { ok: boolean; canceled?: boolean; error?: string; plugins?: CordisStatus[] }

type Bridge = {
  cordisStatus?: () => Promise<CordisStatus[]>;
  cordisReload?: () => Promise<CordisStatus[]>;
  cordisInstall?: () => Promise<CordisResult>;
  cordisUninstall?: (name: string) => Promise<CordisResult>;
  cordisSetEnabled?: (name: string, enabled: boolean) => Promise<CordisResult>;
  onCordisNotify?: (cb: (payload: { plugin: string; level: string; message: string }) => void) => () => void;
  /** 插件集合变化推送（安装 / 启停 / 卸载）：参数是最新的完整列表 */
  onCordisChanged?: (cb: (plugins: CordisStatus[]) => void) => () => void;
};

/** 模块级缓存：插件消息在页面未打开时也会到达，打开页面即可回看 */
const notes: CordisNote[] = [];
const MAX_NOTES = 30;
let listening = false;

function ensureListening() {
  if (listening) return;
  const bridge = (window as unknown as { chatcoderAPI?: Bridge }).chatcoderAPI;
  if (!bridge?.onCordisNotify) return;
  listening = true;
  bridge.onCordisNotify((p) => {
    notes.unshift({ plugin: p.plugin, level: p.level, message: p.message, at: Date.now() });
    if (notes.length > MAX_NOTES) notes.length = MAX_NOTES;
  });
}

const BADGE: Record<CordisStatus["status"], string> = {
  loaded: "已启用",
  disabled: "已停用",
  failed: "加载失败",
  loading: "加载中",
};

export function CordisPluginsSection({ embedded = false, query = "", onCountChange }: {
  /** 嵌入「设置 → 拓展」已安装页时：标题对齐 .inst-group-title、列表对齐 .inst-list，去掉外层间距 */
  embedded?: boolean;
  /** 搜索词（嵌入时由宿主页面传入）：按名称 / 说明过滤 */
  query?: string;
  /** 上报插件总数：宿主页面据此判断是否还有已安装内容，避免与整页空态重复 */
  onCountChange?: (n: number) => void;
} = {}) {
  const bridge = (window as unknown as { chatcoderAPI?: Bridge }).chatcoderAPI;
  const supported = Boolean(bridge?.cordisStatus);
  const [list, setList] = useState<CordisStatus[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [pendingUninstall, setPendingUninstall] = useState<CordisStatus | null>(null);
  /** 嵌入页的条目详情：与「已安装」行同样支持点开查看来源与安装目录 */
  const [detail, setDetail] = useState<CordisStatus | null>(null);

  /** 嵌入宿主页时按搜索词过滤（与页面顶部搜索框同一语义，避免"搜了但列表不变"） */
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter((s) => `${s.name} ${s.description || ""}`.toLowerCase().includes(q));
  }, [list, query]);

  useEffect(() => { onCountChange?.(list.length); }, [list, onCountChange]);

  useEffect(() => {
    ensureListening();
    if (!supported) return;
    void bridge!.cordisStatus!().then(setList).catch(() => setList([]));
    // 安装 / 启停 / 卸载后由主进程推送最新列表：即使操作发生在另一个窗口或页面，这里也会同步
    const off = bridge?.onCordisChanged?.((plugins) => setList(plugins));
    return () => off?.();
  }, [supported]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 写操作统一入口：成功刷新列表，失败在页内给出原因（不打断列表浏览）。 */
  const run = async (label: string, fn: () => Promise<CordisResult>, okText?: string) => {
    setBusy(label);
    setNotice(null);
    try {
      const res = await fn();
      if (res.canceled) return;
      if (!res.ok) {
        setNotice({ kind: "error", text: res.error || "操作失败" });
        return;
      }
      if (res.plugins) setList(res.plugins);
      if (okText) setNotice({ kind: "ok", text: okText });
    } catch (e) {
      setNotice({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  };

  const reload = async () => {
    if (!bridge?.cordisReload) return;
    setBusy("reload");
    try {
      setList(await bridge.cordisReload());
    } finally {
      setBusy(null);
    }
  };

  const loadedCount = list.filter((s) => s.status === "loaded").length;
  const failedCount = list.filter((s) => s.status === "failed").length;

  /** 标题右侧操作组：嵌入与非嵌入共用，避免两处按钮写法漂移 */
  const actions = supported ? (
    <div className="cx-plugins-actions">
      <Button variant="ghost" size="sm" icon={<IconRefresh size={13} />} loading={busy === "reload"}
        disabled={busy != null} onClick={() => void reload()}>
        重新加载
      </Button>
      <Button variant="secondary" size="sm" loading={busy === "install"} disabled={busy != null}
        onClick={() => void run("install", () => bridge!.cordisInstall!(), "插件已安装")}>
        安装插件
      </Button>
    </div>
  ) : null;

  return (
    <div className={`cx-plugins${embedded ? " is-embedded" : ""}`}>
      {embedded ? (
        <div className="cx-plugins-head">
          <div className="cx-plugins-head-title">
            <span>第三方插件（dsh 协议）</span>
            <span className="cx-plugins-count">
              {!supported ? "仅桌面版" : list.length > 0 ? `已启用 ${loadedCount} / ${list.length}` : "未安装"}
            </span>
          </div>
          {actions}
        </div>
      ) : (
        <div className="settings-card-title cx-plugins-title">
          <span>第三方插件（dsh 协议）</span>
          {actions}
        </div>
      )}

      {notice && (
        <div className={`cx-plugins-notice is-${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
          {notice.text}
        </div>
      )}

      {!supported ? (
        <div className={embedded ? "cx-plugins-blank" : "cx-plugins-empty"}>仅桌面版支持安装第三方插件。</div>
      ) : list.length === 0 ? (
        embedded ? (
          <div className="cx-plugins-blank">
            还没有安装第三方插件。点「安装插件」选择插件目录，或 <code>npm pack</code> 生成的 <code>.tgz</code> 包；
            安装前会检查与本软件的兼容性。
          </div>
        ) : (
          <div className="cx-plugins-empty">
            <IconBox size={15} />
            <span>
              还没有安装插件。选择 dsh 第三方插件的目录，或 <code>npm pack</code> 生成的 <code>.tgz</code> 包即可安装；
              安装前会检查它与本软件的兼容性。
            </span>
          </div>
        )
      ) : (
        <>
          {!embedded && (
            <div className="cx-plugins-summary">
              已启用 {loadedCount} 个{failedCount > 0 ? `，${failedCount} 个失败` : ""}
            </div>
          )}
          {embedded && visible.length === 0 ? (
            <div className="cx-plugins-blank">没有匹配「{query.trim()}」的第三方插件。</div>
          ) : embedded ? (
            /* 嵌入「设置 → 拓展」：与已安装行同一套结构与类名——36px 类型瓦片、
               徽标胶囊、说明行、悬停显现的图标操作 + 右侧开关，两种来源条目视觉一致。 */
            <div className="inst-list">
              {visible.map((s) => (
                <div key={s.id} className="inst-row">
                  {/* 可点区只包住图标与文字：行内右侧留白不响应点击 */}
                  <div
                    className="inst-hit"
                    role="button"
                    tabIndex={0}
                    onClick={() => setDetail(s)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDetail(s); }
                    }}
                  >
                    <span className="inst-tile" data-kind="plugin"><IconBox size={16} /></span>
                    <div className="inst-main">
                      <div className="inst-name">
                        <span className="inst-name-text">{s.name}</span>
                        {s.version && <span className="sb-badge">{`v${s.version}`}</span>}
                        <span className={`sb-badge cx-sb-status is-${s.status}`}>{BADGE[s.status]}</span>
                        {s.hasClient && (
                          <Tooltip title="该插件含 dsh 客户端模块，本软件只运行宿主半">
                            <span className="sb-badge">含界面模块</span>
                          </Tooltip>
                        )}
                      </div>
                      <div className={"inst-desc" + (s.description ? "" : " is-empty")}>
                        {s.description || "暂无说明"}
                      </div>
                      {s.error && <div className="cx-plugin-error">{s.error}</div>}
                      {s.warnings?.map((w) => <div key={w} className="cx-plugin-warn">{w}</div>)}
                    </div>
                  </div>
                  <div className="inst-actions">
                    <span className="sb-hover-actions">
                      <Tooltip title="查看详情">
                        <button type="button" className="ui-icon-btn size-xs" aria-label="查看详情"
                          onClick={() => setDetail(s)}>
                          <IconInfo size={14} />
                        </button>
                      </Tooltip>
                      <Tooltip title="卸载">
                        <button type="button" className="ui-icon-btn size-xs tone-danger" aria-label={`卸载 ${s.name}`}
                          disabled={busy != null} onClick={() => setPendingUninstall(s)}>
                          <IconTrash size={14} />
                        </button>
                      </Tooltip>
                    </span>
                    <Switch
                      checked={s.status !== "disabled"}
                      disabled={busy != null}
                      onChange={(v) => void run(`toggle-${s.name}`, () => bridge!.cordisSetEnabled!(s.name, v))}
                      aria-label={`${s.name} 启用状态`}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="cx-plugin-list">
              {visible.map((s) => (
                <div key={s.id} className={`cx-plugin-row is-${s.status}`}>
                  <div className="cx-plugin-main">
                    <div className="cx-plugin-title">
                      <span className="cx-plugin-name">{s.name}</span>
                      {s.version && <span className="cx-plugin-version">v{s.version}</span>}
                      {s.hasClient && (
                        <Tooltip title="该插件含 dsh 客户端模块，本软件只运行宿主半">
                          <span className="cx-plugin-tag">含界面模块</span>
                        </Tooltip>
                      )}
                      <span className={`cx-plugin-badge is-${s.status}`}>{BADGE[s.status]}</span>
                    </div>
                    {s.description && <div className="cx-plugin-desc">{s.description}</div>}
                    {s.error && <div className="cx-plugin-error">{s.error}</div>}
                    {s.warnings?.map((w) => <div key={w} className="cx-plugin-warn">{w}</div>)}
                  </div>
                  <div className="cx-plugin-actions">
                    <Switch
                      checked={s.status !== "disabled"}
                      disabled={busy != null}
                      onChange={(v) => void run(`toggle-${s.name}`, () => bridge!.cordisSetEnabled!(s.name, v))}
                    />
                    <Button variant="danger-ghost" size="sm" disabled={busy != null}
                      onClick={() => setPendingUninstall(s)}>
                      卸载
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {notes.length > 0 && (
        <div className="cx-plugin-notes">
          <div className="cx-plugins-summary">插件消息</div>
          {notes.slice(0, 8).map((n, i) => (
            <div key={`${n.at}-${i}`} className="cx-plugin-note">
              <span className="cx-plugin-note-who">{n.plugin}</span>
              <span className="cx-plugin-note-text">{n.message}</span>
            </div>
          ))}
        </div>
      )}

      {/* 条目详情：与「已安装」行同一套浮层与字段，第三方插件也能查看版本与安装目录 */}
      <Dialog
        open={detail != null}
        onClose={() => setDetail(null)}
        title={
          detail ? (
            <span className="inst-detail-title">
              <span className="inst-tile" data-kind="plugin"><IconBox size={16} /></span>
              <span>{detail.name}</span>
              <span className="inst-kind">插件</span>
            </span>
          ) : "详情"
        }
        subtitle={detail ? `第三方插件（dsh 协议） · ${detail.name}` : undefined}
        width={560}
        footer={detail ? (
          <>
            <button
              className="btn btn-ghost btn-sm"
              disabled={busy != null}
              onClick={() => {
                const enable = detail.status === "disabled";
                void run(`toggle-${detail.name}`, () => bridge!.cordisSetEnabled!(detail.name, enable));
              }}
            >
              {detail.status === "disabled" ? "启用" : "停用"}
            </button>
            <button className="btn btn-danger-ghost btn-sm"
              onClick={() => { setPendingUninstall(detail); setDetail(null); }}>
              <IconTrash size={12} /> 卸载
            </button>
          </>
        ) : null}
      >
        {detail && (
          <div className="inst-detail">
            <p className="inst-detail-desc">{detail.description || "暂无说明"}</p>
            <div className="inst-detail-facts">
              <div className="inst-fact">
                <span className="inst-fact-k">状态</span>
                <span className="inst-fact-v">{BADGE[detail.status]}</span>
              </div>
              {detail.version && (
                <div className="inst-fact">
                  <span className="inst-fact-k">版本</span>
                  <span className="inst-fact-v mono">v{detail.version}</span>
                </div>
              )}
              <div className="inst-fact">
                <span className="inst-fact-k">来源</span>
                <span className="inst-fact-v">第三方插件（dsh 协议）</span>
              </div>
              <div className="inst-fact">
                <span className="inst-fact-k">本机位置</span>
                <span className="inst-fact-v mono">{detail.dir}</span>
              </div>
              {detail.hasClient && (
                <div className="inst-fact">
                  <span className="inst-fact-k">界面模块</span>
                  <span className="inst-fact-v">含 dsh 客户端模块，本软件只运行宿主半</span>
                </div>
              )}
            </div>
            {detail.error && <div className="cx-plugin-error">{detail.error}</div>}
            {detail.warnings?.map((w) => <div key={w} className="cx-plugin-warn">{w}</div>)}
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={pendingUninstall != null}
        title="卸载插件"
        message={pendingUninstall ? `将删除插件「${pendingUninstall.name}」的安装目录，之后需要重新安装才能使用。确定卸载吗？` : ""}
        confirmLabel="卸载"
        danger
        busy={busy === "uninstall"}
        onCancel={() => setPendingUninstall(null)}
        onConfirm={() => {
          const target = pendingUninstall;
          if (!target) return;
          void run("uninstall", () => bridge!.cordisUninstall!(target.name), `已卸载 ${target.name}`)
            .finally(() => setPendingUninstall(null));
        }}
      />
    </div>
  );
}
