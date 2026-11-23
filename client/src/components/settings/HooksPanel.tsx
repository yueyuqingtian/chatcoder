/** 设置中心：钩子（v2.2 对齐 zcode 3.18；S12 重写 plan-41-197）。
 *
 * 钩子 = 在任务执行期间按「事件」触发的动作，支持两种类型：
 *  - 注入提示词：命中事件时把一段提示词注入给 AI（如"调用终端前先确认工作目录"）；
 *  - 执行命令：运行外部脚本（事件载荷经 stdin 以 JSON 传入，stdout 可回传决策）。
 * 触发事件：工具调用前 / 工具调用后 / 用户消息提交时 / 权限审批请求时；
 * 可选 matcher 按工具名过滤（如 fs_write、terminal_exec），留空 = 所有工具。
 *
 * S12 之前该页面只有「列表 + 开关 + 删除」（GenericPanel），无法新建，
 * 实际上是个空页面；本次补齐完整配置能力并接上后端触发链路。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type HookConfigOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconAnchor, IconPencil, IconPlus, IconRefresh, IconX } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Button, Empty, IconButton, Input, PageActions, Section, Segmented, Switch, Tag, Textarea, Tooltip } from "../ui";

/** 事件中文名与说明（列表与新建弹窗共用） */
const HOOK_EVENTS: Array<{ value: string; label: string; desc: string }> = [
  { value: "pre_tool_use", label: "工具调用前", desc: "AI 即将调用某个工具时触发；配合「工具匹配」可只对指定工具生效" },
  { value: "post_tool_use", label: "工具调用后", desc: "工具执行完成后触发（含失败）" },
  { value: "user_prompt_submit", label: "用户消息提交时", desc: "每轮任务开始时触发" },
  { value: "permission_request", label: "权限审批请求时", desc: "AI 请求审批（命令 / 写盘）时触发" },
];

const eventLabel = (v: string) => HOOK_EVENTS.find((e) => e.value === v)?.label || v;

function notify(msg: string) { useChatStore.setState({ error: msg }); }

export function HooksPanel() {
  const [items, setItems] = useState<HookConfigOut[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<HookConfigOut | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<HookConfigOut | null>(null);

  const load = useCallback(async () => {
    try { setItems(await api.listHooks()); } catch (e) { notify("加载钩子失败：" + String(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  return (
    <div className="sc-page">
      <PageActions>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} onClick={() => void load()}>刷新</Button>
        <Button variant="primary" size="sm" icon={<IconPlus size={13} />} onClick={() => { setEditing(null); setShowForm(true); }}>新建钩子</Button>
      </PageActions>

      <div className="sched-note">
        在任务执行期间按事件触发动作：向 AI 注入提示词，或执行外部命令（载荷经 stdin 以 JSON 传入）。
      </div>

      <Section title={items.length > 0 ? `已配置钩子（${items.length}）` : undefined} bare>
        <div className="settings-resource-list">
          {items.map((h) => (
            <div key={h.id} className={"settings-resource-item sb-item" + (h.enabled ? "" : " is-off")}>
              <span className="sb-tile" aria-hidden><IconAnchor size={16} /></span>
              <div className="settings-resource-info">
                <div className="settings-resource-name">
                  {/* plan-91-393：事件以徽标呈现，类型次级标签 */}
                  <span className="sb-badge accent">{eventLabel(h.event)}</span>
                  <span className="hook-type-tag">{h.hook_type === "command" ? "执行命令" : "注入提示词"}</span>
                </div>
                {h.matcher && (
                  <div className="settings-resource-desc">
                    <Tag>匹配 <code className="sb-mono">{h.matcher}</code></Tag>
                  </div>
                )}
                <Tooltip content={h.hook_type === "command" ? h.command : (h.prompt || "")}>
                  <div className={"sb-preview" + (h.hook_type === "command" ? " sb-mono" : "")}>
                    {h.hook_type === "command" ? h.command : (h.prompt || "")}
                  </div>
                </Tooltip>
              </div>
              <div className="settings-resource-actions">
                <Switch checked={h.enabled} aria-label={`启用钩子 ${eventLabel(h.event)}`} onChange={async (v) => {
                  try { await api.updateHook(h.id, { enabled: v }); await load(); }
                  catch (e) { notify("更新失败：" + String(e)); }
                }} />
                <span className="sb-hover-actions">
                  <IconButton size="xs" title="编辑" aria-label="编辑" icon={<IconPencil size={12} />} onClick={() => { setEditing(h); setShowForm(true); }} />
                  <IconButton size="xs" tone="danger" title="删除" aria-label="删除" icon={<IconX size={12} />} onClick={() => setConfirmTarget(h)} />
                </span>
              </div>
            </div>
          ))}
          {items.length === 0 && (
            <Empty title="暂无钩子" desc="例如在调用终端前注入一段路径约定提示词"
              action={<Button variant="secondary" size="sm" icon={<IconPlus size={13} />} onClick={() => { setEditing(null); setShowForm(true); }}>新建钩子</Button>} />
          )}
        </div>
      </Section>

      <HookFormModal open={showForm} editing={editing} onClose={() => setShowForm(false)} onSaved={load} />

      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除钩子"
        message={confirmTarget ? `删除该钩子（${eventLabel(confirmTarget.event)}）？` : ""}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={async () => {
          const it = confirmTarget;
          setConfirmTarget(null);
          if (!it) return;
          try { await api.deleteHook(it.id); await load(); } catch (e) { notify(String(e)); }
        }}
      />
    </div>
  );
}

/** 新建 / 编辑弹窗 */
function HookFormModal({ open, editing, onClose, onSaved }: {
  open: boolean; editing: HookConfigOut | null; onClose: () => void; onSaved: () => void;
}) {
  const [form, setForm] = useState({
    event: "pre_tool_use",
    hook_type: "prompt" as "command" | "prompt",
    matcher: "",
    command: "",
    prompt: "",
    enabled: true,
  });

  useEffect(() => {
    if (!open) return;
    if (editing) {
      setForm({
        event: editing.event,
        hook_type: editing.hook_type === "command" ? "command" : "prompt",
        matcher: editing.matcher || "",
        command: editing.command || "",
        prompt: editing.prompt || "",
        enabled: editing.enabled,
      });
    } else {
      setForm({ event: "pre_tool_use", hook_type: "prompt", matcher: "", command: "", prompt: "", enabled: true });
    }
  }, [open, editing]);

  const valid = form.hook_type === "prompt" ? form.prompt.trim().length > 0 : form.command.trim().length > 0;

  const handleSave = async () => {
    if (!valid) return;
    try {
      const data = {
        event: form.event,
        hook_type: form.hook_type,
        matcher: form.matcher.trim() || undefined,
        command: form.command.trim(),
        prompt: form.hook_type === "prompt" ? form.prompt.trim() : undefined,
        enabled: form.enabled,
      };
      if (editing) await api.updateHook(editing.id, data);
      else await api.createHook(data);
      onSaved();
      onClose();
    } catch (e) { notify("保存失败：" + String(e)); }
  };

  const evDesc = HOOK_EVENTS.find((e) => e.value === form.event)?.desc || "";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={editing ? "编辑钩子" : "新建钩子"}
      subtitle="在任务执行期间按事件触发动作"
      width={620}
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={onClose}>取消</Button>
          <Button variant="primary" size="sm" disabled={!valid} onClick={() => void handleSave()}>
            {editing ? "保存" : "创建"}
          </Button>
        </>
      }
    >
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row">
          <label>触发事件</label>
          <Segmented<string>
            size="sm"
            value={form.event}
            onChange={(v) => setForm((p) => ({ ...p, event: v }))}
            aria-label="触发事件"
            options={HOOK_EVENTS.map((e) => ({ value: e.value, label: e.label }))}
          />
          <div className="ui-field-hint">{evDesc}</div>
        </div>
        <div className="settings-modal-form-row">
          <label>工具匹配（可选，仅工具类事件生效）</label>
          <Input
            placeholder="如 fs_write / terminal_exec；留空 = 所有工具"
            value={form.matcher}
            onChange={(e) => setForm((p) => ({ ...p, matcher: e.target.value }))}
          />
        </div>
        <div className="settings-modal-form-row">
          <label>动作类型</label>
          <Segmented<"prompt" | "command">
            size="sm"
            value={form.hook_type}
            onChange={(v) => setForm((p) => ({ ...p, hook_type: v }))}
            aria-label="动作类型"
            options={[
              { value: "prompt", label: "注入提示词" },
              { value: "command", label: "执行命令" },
            ]}
          />
        </div>
        {form.hook_type === "prompt" ? (
          <div className="settings-modal-form-row">
            <label>注入的提示词</label>
            <Textarea
              rows={5}
              placeholder="命中事件时把这段文本注入给 AI。例如：调用终端前先确认当前目录在项目根下。"
              value={form.prompt}
              onChange={(e) => setForm((p) => ({ ...p, prompt: e.target.value }))}
              aria-label="注入的提示词"
            />
          </div>
        ) : (
          <div className="settings-modal-form-row">
            <label>执行的命令</label>
            <Input
              placeholder="如 node scripts/check.js"
              value={form.command}
              onChange={(e) => setForm((p) => ({ ...p, command: e.target.value }))}
            />
            <div className="ui-field-hint">事件载荷经 stdin 以 JSON 传入；stdout 输出 JSON 可回传决策；执行失败不阻断任务（fail-open）</div>
          </div>
        )}
        <div className="settings-modal-form-row">
          <label>启用状态</label>
          <Switch checked={form.enabled} onChange={(v) => setForm((p) => ({ ...p, enabled: v }))} />
        </div>
      </div>
    </Modal>
  );
}
