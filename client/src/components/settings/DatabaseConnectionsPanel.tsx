/** DatabaseConnectionsPanel —— 数据库连接配置（plan-282-1441 #7）。
 *
 * 作为内置 MCP「数据库连接」的详情页，嵌入「拓展 → 连接器」中：
 *  - 顶部：内置 MCP 的启用开关（默认关闭，用户手动开启）
 *  - 按**当前项目**管理连接（主机/端口/库/账号/密码 + 测试连接）
 *  - 权限区：读 / 写 / DDL 三个开关 + 是否强制审批 + 行数上限 / 超时
 *
 * 安全说明：权限由**服务端强制门控**（db_connection_service.check_permission），
 * 这里的开关只是配置入口，不是安全边界。
 */
import { useCallback, useEffect, useState } from "react";
import {
  api, type DbConnectionOut, type DbPolicyOut, type McpServerOut, type ProjectOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { ConfirmDialog } from "../ConfirmDialog";
import { FormDialog } from "../ui/FormDialog";
import { Input, Select, Switch } from "../ui";
import { IconDatabase, IconPlus, IconRefresh, IconTrash } from "../icons";

/** 各数据库默认端口 */
const DEFAULT_PORTS: Record<string, number> = { mysql: 3306, postgresql: 5432, sqlserver: 1433 };

const KIND_OPTIONS = [
  { value: "mysql", label: "MySQL / MariaDB" },
  { value: "postgresql", label: "PostgreSQL" },
  { value: "sqlserver", label: "SQL Server" },
];

export function DatabaseConnectionsPanel({ server }: { server?: McpServerOut }) {
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const [projectId, setProjectId] = useState<number | null>(currentProjectId ?? null);
  const [conns, setConns] = useState<DbConnectionOut[]>([]);
  const [policy, setPolicy] = useState<DbPolicyOut | null>(null);
  /** 权限策略草稿：改完点「保存」才写库。
   *  此前每次按键/开关都发一次 PUT——不仅看不到保存动作，还会因请求乱序
   *  让迟到的响应把输入框改回旧值（表现为"改了又被改回去"）。 */
  const [policyDraft, setPolicyDraft] = useState<DbPolicyOut | null>(null);
  const [policySaving, setPolicySaving] = useState(false);
  const [policyMsg, setPolicyMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testMsg, setTestMsg] = useState<Record<number, string>>({});
  /** 表单 */
  const [editing, setEditing] = useState<DbConnectionOut | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "", kind: "mysql", host: "127.0.0.1", port: 3306,
    database: "", username: "", password: "",
  });
  const [dropTarget, setDropTarget] = useState<DbConnectionOut | null>(null);

  const activeProjects = projects.filter((p) => !p.archived);
  const effectiveProjectId = projectId ?? activeProjects[0]?.id ?? null;

  const load = useCallback(async () => {
    if (effectiveProjectId == null) return;
    setBusy(true);
    try {
      const [list, pol] = await Promise.all([
        api.listDbConnections(effectiveProjectId),
        api.getDbPolicy(effectiveProjectId),
      ]);
      setConns(list);
      setPolicy(pol);
      setPolicyDraft(pol);
      setPolicyMsg(null);
    } catch (e) {
      useChatStore.setState({ error: `加载数据库连接失败：${String(e)}` });
    } finally { setBusy(false); }
  }, [effectiveProjectId]);
  useEffect(() => { void load(); }, [load]);

  const openCreate = () => {
    setEditing(null);
    setForm({ name: "", kind: "mysql", host: "127.0.0.1", port: 3306, database: "", username: "", password: "" });
    setShowForm(true);
  };

  const openEdit = (c: DbConnectionOut) => {
    setEditing(c);
    setForm({
      name: c.name, kind: c.kind, host: c.host,
      port: c.port || DEFAULT_PORTS[c.kind] || 0,
      database: c.database ?? "", username: c.username ?? "", password: "",
    });
    setShowForm(true);
  };

  const submit = async () => {
    if (effectiveProjectId == null) return;
    setBusy(true);
    try {
      if (editing) {
        await api.updateDbConnection(editing.id, {
          name: form.name, kind: form.kind, host: form.host, port: form.port,
          database: form.database, username: form.username,
          // 留空表示"不改密码"——避免编辑时误清空
          ...(form.password ? { password: form.password } : {}),
        });
      } else {
        await api.createDbConnection({
          project_id: effectiveProjectId, name: form.name, kind: form.kind,
          host: form.host, port: form.port, database: form.database,
          username: form.username, password: form.password,
        });
      }
      setShowForm(false);
      await load();
    } catch (e) {
      useChatStore.setState({ error: `保存失败：${String(e)}` });
    } finally { setBusy(false); }
  };

  const toggleActive = async (c: DbConnectionOut, v: boolean) => {
    try {
      await api.updateDbConnection(c.id, { is_active: v });
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  const testOne = async (c: DbConnectionOut) => {
    setTestMsg((m) => ({ ...m, [c.id]: "测试中…" }));
    try {
      const res = await api.testDbConnection(c.id);
      setTestMsg((m) => ({
        ...m,
        [c.id]: res.ok ? `连接成功（${res.server || "OK"}）` : `失败：${res.error || "未知原因"}`,
      }));
    } catch (e) {
      setTestMsg((m) => ({ ...m, [c.id]: `失败：${String(e)}` }));
    }
  };

  const remove = async () => {
    if (!dropTarget) return;
    try {
      await api.deleteDbConnection(dropTarget.id);
      setDropTarget(null);
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  /** 策略是否有未保存修改 */
  const policyDirty = Boolean(policy && policyDraft && (
    policy.allow_read !== policyDraft.allow_read
    || policy.allow_write !== policyDraft.allow_write
    || policy.allow_ddl !== policyDraft.allow_ddl
    || policy.require_approval !== policyDraft.require_approval
    || policy.row_limit !== policyDraft.row_limit
    || policy.timeout_s !== policyDraft.timeout_s
  ));

  /** 草稿修改（不动库）；保存时才 PUT 一次 */
  const patchDraft = (patch: Partial<DbPolicyOut>) => {
    setPolicyMsg(null);
    setPolicyDraft((prev) => (prev ? { ...prev, ...patch } : prev));
  };

  const savePolicy = async () => {
    if (effectiveProjectId == null || !policyDraft) return;
    setPolicySaving(true);
    setPolicyMsg(null);
    try {
      const next = await api.setDbPolicy(effectiveProjectId, {
        allow_read: policyDraft.allow_read,
        allow_write: policyDraft.allow_write,
        allow_ddl: policyDraft.allow_ddl,
        require_approval: policyDraft.require_approval,
        row_limit: policyDraft.row_limit,
        timeout_s: policyDraft.timeout_s,
      });
      setPolicy(next);
      setPolicyDraft(next);
      setPolicyMsg("已保存到数据库");
    } catch (e) {
      useChatStore.setState({ error: `保存权限策略失败：${String(e)}` });
      setPolicyMsg("保存失败，请重试");
    } finally { setPolicySaving(false); }
  };

  const toggleServer = async (v: boolean) => {
    if (!server) return;
    try {
      await api.updateMcpServer(server.id, { is_active: v });
      await useChatStore.getState().loadBootstrap();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  return (
    <div className="db-panel">
      {/* 内置 MCP 总开关 */}
      {server && (
        <section className="db-section db-master">
          <div className="db-master-info">
            <div className="db-master-title"><IconDatabase size={14} /> {server.display_name || server.name}</div>
            <div className="db-master-desc">{server.description || "让 AI 连接项目数据库执行查询与变更。"}</div>
          </div>
          <Switch checked={server.is_active} onChange={(v) => void toggleServer(v)} />
        </section>
      )}

      {/* 项目选择 */}
      <section className="db-section">
        <div className="db-row">
          <span className="db-label">项目</span>
          <div className="db-controls">
            <Select
              value={effectiveProjectId != null ? String(effectiveProjectId) : ""}
              onChange={(v) => setProjectId(Number(v))}
              options={activeProjects.map((p: ProjectOut) => ({
                value: String(p.id), label: p.name || p.path,
              }))}
              className="db-project-select"
              aria-label="选择项目"
            />
            <button className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={busy} title="重新加载">
              <IconRefresh size={13} />
            </button>
          </div>
        </div>
        <div className="db-hint">连接与权限按项目隔离：AI 在某个项目工作时只能看到该项目的连接。</div>
      </section>

      {/* 连接列表：空态改为紧凑可操作空态（含主操作），不再用 40px 内距的 navpage-empty
          形成大块荒芜，也避免与 section 头部按钮重复表达同一动作。 */}
      <section className="db-section">
        <div className="db-section-head">
          <span>连接（{conns.length}）</span>
          {conns.length > 0 && (
            <button className="btn btn-primary btn-sm" onClick={openCreate} disabled={effectiveProjectId == null}>
              <IconPlus size={13} /> 添加连接
            </button>
          )}
        </div>
        {conns.length === 0 && (
          <div className="db-empty">
            <IconDatabase size={16} />
            <span className="db-empty-text">该项目还没有数据库连接，添加后 AI 才能查询或变更该库。</span>
            <button className="btn btn-primary btn-sm" onClick={openCreate} disabled={effectiveProjectId == null}>
              <IconPlus size={13} /> 添加连接
            </button>
          </div>
        )}
        {conns.map((c) => (
          <div className="settings-resource-item db-conn-item" key={c.id}>
            <div className="settings-resource-info">
              <div className="settings-resource-name">
                {c.name}
                <span className="db-kind">{KIND_OPTIONS.find((k) => k.value === c.kind)?.label || c.kind}</span>
                {c.has_password && <span className="db-kind">已设密码</span>}
              </div>
              <div className="settings-resource-desc">
                {c.host}:{c.port} / {c.database || "（未指定库）"}　账号 {c.username || "—"}
              </div>
              {testMsg[c.id] && (
                <div className={`db-test${testMsg[c.id].startsWith("连接成功") ? " ok" : ""}`}>{testMsg[c.id]}</div>
              )}
            </div>
            <div className="settings-resource-actions db-conn-actions">
              <Switch checked={c.is_active} onChange={(v) => void toggleActive(c, v)} />
              <button className="btn btn-ghost btn-xs" onClick={() => void testOne(c)} disabled={busy}>测试</button>
              <button className="btn btn-ghost btn-xs" onClick={() => openEdit(c)}>编辑</button>
              <button className="btn btn-danger btn-xs" onClick={() => setDropTarget(c)} title="删除连接">
                <IconTrash size={12} />
              </button>
            </div>
          </div>
        ))}
      </section>

      {/* 权限策略（草稿 + 保存，避免逐键写入与请求乱序） */}
      {policy && policyDraft && (
        <section className="db-section">
          <div className="db-section-head"><span>AI 操作权限</span></div>
          <div className="db-hint">
            以下权限由服务端强制校验：即使 AI 尝试越权，也会在执行前被拒绝。
            修改后需点下方「保存」才会写入数据库并生效。
          </div>
          <div className="db-policy-row">
            <span>允许读取（SELECT / 表结构）</span>
            <Switch checked={policyDraft.allow_read} onChange={(v) => patchDraft({ allow_read: v })} />
          </div>
          <div className="db-policy-row">
            <span>允许写入（INSERT / UPDATE / DELETE）</span>
            <Switch checked={policyDraft.allow_write} onChange={(v) => patchDraft({ allow_write: v })} />
          </div>
          <div className="db-policy-row">
            <span className="danger">允许 DDL（CREATE / ALTER / DROP / TRUNCATE）</span>
            <Switch checked={policyDraft.allow_ddl} onChange={(v) => patchDraft({ allow_ddl: v })} />
          </div>
          <div className="db-policy-row">
            <span>执行前需要我审批</span>
            <Switch checked={policyDraft.require_approval}
              onChange={(v) => patchDraft({ require_approval: v })} />
          </div>
          {/* 数值行：标签 + 控件列（输入框由 .db-num 显式控宽，不再被 100% 拉满整行） */}
          <div className="db-form-row">
            <span className="db-label">单次查询最多返回行数</span>
            <div className="db-controls">
              <Input
                type="number"
                value={String(policyDraft.row_limit)}
                onChange={(e) => patchDraft({ row_limit: Number(e.target.value) || 200 })}
                className="db-num"
                aria-label="行数上限"
              />
            </div>
          </div>
          <div className="db-form-row">
            <span className="db-label">执行超时（秒）</span>
            <div className="db-controls">
              <Input
                type="number"
                value={String(policyDraft.timeout_s)}
                onChange={(e) => patchDraft({ timeout_s: Number(e.target.value) || 15 })}
                className="db-num"
                aria-label="超时秒数"
              />
            </div>
          </div>
          {/* 保存条归位为卡片底栏：与本 section 同宽、带上分隔线，明确"改完在这里保存" */}
          <div className="dbg-savebar">
            <span className={`dbg-savebar-hint${policyDirty ? " dirty" : ""}`}>
              {policySaving ? "保存中…" : policyDirty ? "有未保存的修改" : (policyMsg || "已保存")}
            </span>
            <button className="btn btn-primary btn-sm" onClick={() => void savePolicy()}
              disabled={!policyDirty || policySaving}>
              保存
            </button>
          </div>
        </section>
      )}

      {/* 新建 / 编辑 */}
      <FormDialog
        open={showForm}
        onClose={() => setShowForm(false)}
        title={editing ? `编辑连接「${editing.name}」` : "添加数据库连接"}
        submitLabel="保存"
        onSubmit={() => void submit()}
        submitDisabled={!form.name.trim() || !form.host.trim() || busy}
      >
        <Input placeholder="连接名称（如 本地开发库）" value={form.name}
          onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} aria-label="连接名称" />
        <Select value={form.kind} aria-label="数据库类型"
          onChange={(v) => setForm((p) => ({ ...p, kind: v, port: DEFAULT_PORTS[v] ?? p.port }))}
          options={KIND_OPTIONS} />
        <Input placeholder="主机（如 127.0.0.1）" value={form.host}
          onChange={(e) => setForm((p) => ({ ...p, host: e.target.value }))} aria-label="主机" />
        <Input type="number" placeholder="端口" value={String(form.port)}
          onChange={(e) => setForm((p) => ({ ...p, port: Number(e.target.value) || 0 }))} aria-label="端口" />
        <Input placeholder="数据库名" value={form.database}
          onChange={(e) => setForm((p) => ({ ...p, database: e.target.value }))} aria-label="数据库名" />
        <Input placeholder="账号" value={form.username}
          onChange={(e) => setForm((p) => ({ ...p, username: e.target.value }))} aria-label="账号" />
        <Input type="password"
          placeholder={editing ? "密码（留空表示不修改）" : "密码"}
          value={form.password}
          onChange={(e) => setForm((p) => ({ ...p, password: e.target.value }))} aria-label="密码" />
      </FormDialog>

      <ConfirmDialog
        open={dropTarget != null}
        title="删除数据库连接"
        message={`将删除连接「${dropTarget?.name ?? ""}」及其配置（数据库本身不受影响）。`}
        confirmLabel="删除"
        danger
        onCancel={() => setDropTarget(null)}
        onConfirm={() => void remove()}
      />
    </div>
  );
}
