/** DatabaseConnectionsPanel —— 数据库连接配置（plan-282-1441 #7）。
 *
 * 作为内置 MCP「数据库连接」的详情页，嵌入「拓展 → 连接器」的配置弹窗中：
 *  - 顶部：内置 MCP 的启用开关（默认关闭，用户手动开启）
 *  - 按**当前项目**管理连接（主机/端口/库/账号/密码 + 测试连接）
 *  - 权限区：读 / 写 / DDL 三个开关 + 是否强制审批 + 行数上限 / 超时
 *
 * 安全说明：权限由**服务端强制门控**（db_connection_service.check_permission），
 * 这里的开关只是配置入口，不是安全边界。
 *
 * 排版（plan-20261017-goal12 项 10）：分组卡 + 行内控件，连接行的操作收成图标按钮，
 * 连接表单改为两列栅格、每个字段有标签与说明；外壳见 ConnectorShell。
 */
import { useCallback, useEffect, useState } from "react";
import {
  api, type DbConnectionOut, type DbPolicyOut, type McpServerOut, type ProjectOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { ConfirmDialog } from "../ConfirmDialog";
import { FormDialog } from "../ui/FormDialog";
import { Button, DialogSection, Field, IconButton, InsetCard, InsetRow, Input, Select, Switch, Tooltip } from "../ui";
import { IconDatabase, IconPencil, IconPlug, IconPlus, IconRefresh, IconTrash } from "../icons";
import { ConnectorMaster, ConnectorSaveBar, ConnectorSwitchRow } from "./ConnectorShell";

/** 各数据库默认端口 */
const DEFAULT_PORTS: Record<string, number> = { mysql: 3306, postgresql: 5432, sqlserver: 1433 };

const KIND_OPTIONS = [
  { value: "mysql", label: "MySQL / MariaDB" },
  { value: "postgresql", label: "PostgreSQL" },
  { value: "sqlserver", label: "SQL Server" },
];

const EMPTY_FORM = { name: "", kind: "mysql", host: "127.0.0.1", port: 3306, database: "", username: "", password: "" };

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
  const [form, setForm] = useState(EMPTY_FORM);
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
    setForm(EMPTY_FORM);
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

  const kindLabel = (kind: string) => KIND_OPTIONS.find((k) => k.value === kind)?.label || kind;

  return (
    <div className="cx-panel">
      {server && (
        <ConnectorMaster
          icon={<IconDatabase size={16} />}
          title={server.display_name || server.name}
          desc={server.description || "让 AI 连接项目数据库执行查询与变更。"}
          checked={server.is_active}
          onChange={(v) => void toggleServer(v)}
        />
      )}

      <DialogSection title="项目" aside="连接与权限按项目隔离">
        <InsetCard>
          <InsetRow
            title="当前项目"
            desc="AI 在某个项目工作时，只能看到该项目的连接"
            actions={(
              <>
                <Select
                  value={effectiveProjectId != null ? String(effectiveProjectId) : ""}
                  onChange={(v) => setProjectId(Number(v))}
                  options={activeProjects.map((p: ProjectOut) => ({ value: String(p.id), label: p.name || p.path }))}
                  className="cx-project-select"
                  aria-label="选择项目"
                />
                <Tooltip content="重新加载" side="top">
                  <IconButton icon={<IconRefresh size={14} />} aria-label="重新加载" onClick={() => void load()} disabled={busy} />
                </Tooltip>
              </>
            )}
          />
        </InsetCard>
      </DialogSection>

      <DialogSection
        title={`连接（${conns.length}）`}
        aside={conns.length > 0 && (
          <Button variant="secondary" size="sm" icon={<IconPlus size={13} />} onClick={openCreate} disabled={effectiveProjectId == null}>
            添加连接
          </Button>
        )}
      >
        <InsetCard>
          {conns.length === 0 ? (
            <div className="cx-empty">
              <span className="cx-empty-text">该项目还没有数据库连接，添加后 AI 才能查询或变更该库。</span>
              <Button variant="primary" size="sm" icon={<IconPlus size={13} />} onClick={openCreate} disabled={effectiveProjectId == null}>
                添加连接
              </Button>
            </div>
          ) : conns.map((c) => (
            <div className="cx-conn" key={c.id}>
              <div className="cx-conn-main">
                <div className="cx-conn-title">
                  <span className="cx-conn-name">{c.name}</span>
                  <span className="cx-tag">{kindLabel(c.kind)}</span>
                  {c.has_password && <span className="cx-tag">已设密码</span>}
                </div>
                <div className="cx-conn-addr">
                  {c.host}:{c.port} · {c.database || "未指定数据库"} · 账号 {c.username || "—"}
                </div>
                {testMsg[c.id] && (
                  <div className={`cx-conn-test${testMsg[c.id].startsWith("连接成功") ? " is-ok" : testMsg[c.id].startsWith("失败") ? " is-error" : ""}`}>
                    {testMsg[c.id]}
                  </div>
                )}
              </div>
              <div className="cx-conn-ops">
                <Switch checked={c.is_active} aria-label={`${c.name} 启用`} onChange={(v) => void toggleActive(c, v)} />
                <Tooltip content="测试连接" side="top">
                  <IconButton icon={<IconPlug size={14} />} aria-label="测试连接" onClick={() => void testOne(c)} disabled={busy} />
                </Tooltip>
                <Tooltip content="编辑" side="top">
                  <IconButton icon={<IconPencil size={14} />} aria-label="编辑连接" onClick={() => openEdit(c)} />
                </Tooltip>
                <Tooltip content="删除连接" side="top">
                  <IconButton icon={<IconTrash size={14} />} aria-label="删除连接" tone="danger" onClick={() => setDropTarget(c)} />
                </Tooltip>
              </div>
            </div>
          ))}
        </InsetCard>
      </DialogSection>

      {policy && policyDraft && (
        <DialogSection title="AI 操作权限" aside="服务端强制校验">
          <InsetCard>
            <ConnectorSwitchRow title="允许读取" desc="SELECT 查询与表结构" checked={policyDraft.allow_read} onChange={(v) => patchDraft({ allow_read: v })} />
            <ConnectorSwitchRow title="允许写入" desc="INSERT / UPDATE / DELETE" checked={policyDraft.allow_write} onChange={(v) => patchDraft({ allow_write: v })} />
            <ConnectorSwitchRow title="允许 DDL" desc="CREATE / ALTER / DROP / TRUNCATE，风险最高" danger checked={policyDraft.allow_ddl} onChange={(v) => patchDraft({ allow_ddl: v })} />
            <ConnectorSwitchRow title="执行前需要我审批" desc="每条语句执行前先经你确认" checked={policyDraft.require_approval} onChange={(v) => patchDraft({ require_approval: v })} />
          </InsetCard>
          <InsetCard>
            <div className="cx-limits">
              <Field label="单次返回行数上限">
                <Input
                  type="number"
                  value={String(policyDraft.row_limit)}
                  onChange={(e) => patchDraft({ row_limit: Number(e.target.value) || 200 })}
                  aria-label="行数上限"
                />
              </Field>
              <Field label="执行超时（秒）">
                <Input
                  type="number"
                  value={String(policyDraft.timeout_s)}
                  onChange={(e) => patchDraft({ timeout_s: Number(e.target.value) || 15 })}
                  aria-label="超时秒数"
                />
              </Field>
            </div>
          </InsetCard>
          <ConnectorSaveBar
            dirty={policyDirty}
            saving={policySaving}
            idleText={policyMsg || "修改后点保存才会生效"}
            onSave={() => void savePolicy()}
          />
        </DialogSection>
      )}

      {/* 新建 / 编辑：两列栅格，每个字段有标签；编辑时密码留空即不修改 */}
      <FormDialog
        open={showForm}
        onClose={() => setShowForm(false)}
        title={editing ? `编辑连接「${editing.name}」` : "添加数据库连接"}
        subtitle="凭据只保存在本机数据库中，界面不会回显已保存的密码"
        submitLabel="保存"
        onSubmit={() => void submit()}
        submitDisabled={!form.name.trim() || !form.host.trim() || busy}
        width={600}
      >
        <div className="cx-form">
          <Field label="连接名称" required>
            <Input placeholder="如：本地开发库" value={form.name} aria-label="连接名称"
              onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          </Field>
          <Field label="数据库类型">
            <Select value={form.kind} aria-label="数据库类型"
              onChange={(v) => setForm((p) => ({ ...p, kind: v, port: DEFAULT_PORTS[v] ?? p.port }))}
              options={KIND_OPTIONS} />
          </Field>
          <div className="cx-form-grid is-address">
            <Field label="主机" required>
              <Input placeholder="127.0.0.1" value={form.host} aria-label="主机"
                onChange={(e) => setForm((p) => ({ ...p, host: e.target.value }))} />
            </Field>
            <Field label="端口">
              <Input type="number" placeholder="端口" value={String(form.port)} aria-label="端口"
                onChange={(e) => setForm((p) => ({ ...p, port: Number(e.target.value) || 0 }))} />
            </Field>
          </div>
          <Field label="数据库名" hint="可留空，连接后由 AI 指定">
            <Input placeholder="如：app_dev" value={form.database} aria-label="数据库名"
              onChange={(e) => setForm((p) => ({ ...p, database: e.target.value }))} />
          </Field>
          <div className="cx-form-grid">
            <Field label="账号">
              <Input placeholder="如：root" value={form.username} aria-label="账号"
                onChange={(e) => setForm((p) => ({ ...p, username: e.target.value }))} />
            </Field>
            <Field label="密码" hint={editing ? "留空表示不修改已保存的密码" : undefined}>
              <Input type="password" placeholder={editing ? "不修改请留空" : "密码"} value={form.password} aria-label="密码"
                onChange={(e) => setForm((p) => ({ ...p, password: e.target.value }))} />
            </Field>
          </div>
        </div>
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
