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
import { IconPlus, IconRefresh, IconX } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Input, Textarea } from "../ui";
import { Sw } from "./shared";

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
    <div>
      <div className="rules-head">
        <span className="rules-head-hint">
          在任务执行期间按事件触发动作：向 AI 注入提示词，或执行外部命令（载荷经 stdin 以 JSON 传入）。
        </span>
        <div className="rules-head-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => void load()}><IconRefresh size={13} /> 刷新</button>
          <button className="btn btn-primary btn-sm" onClick={() => { setEditing(null); setShowForm(true); }}>
            <IconPlus size={13} /> 新建钩子
          </button>
        </div>
      </div>

      <div className="settings-resource-list">
        {items.map((h) => (
          <div key={h.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">
                {eventLabel(h.event)}
                <span className="hook-type-tag">{h.hook_type === "command" ? "执行命令" : "注入提示词"}</span>
              </div>
              <div className="settings-resource-desc">
                {h.matcher && <span className="settings-resource-tag">工具匹配 {h.matcher}</span>}
                <span
                  className="hook-content"
                  title={h.hook_type === "command" ? h.command : (h.prompt || "")}
                >
                  {h.hook_type === "command" ? h.command : (h.prompt || "")}
                </span>
              </div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={h.enabled} onChange={async (v) => {
                try { await api.updateHook(h.id, { enabled: v }); await load(); }
                catch (e) { notify("更新失败：" + String(e)); }
              }} />
              <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(h); setShowForm(true); }}>编辑</button>
              <button className="btn btn-ghost btn-xs" aria-label="删除" onClick={() => setConfirmTarget(h)}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && (
          <div className="navpage-empty">
            暂无钩子。点击右上「新建钩子」配置第一条：例如在调用终端前注入一段路径约定提示词。
          </div>
        )}
      </div>

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
          <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
          <button className="btn btn-primary btn-sm" disabled={!valid} onClick={() => void handleSave()}>
            {editing ? "保存" : "创建"}
          </button>
        </>
      }
    >
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row">
          <label>触发事件</label>
          <div className="settings-chips">
            {HOOK_EVENTS.map((e) => (
              <button key={e.value} type="button"
                className={"settings-chip" + (form.event === e.value ? " on" : "")}
                title={e.desc}
                onClick={() => setForm((p) => ({ ...p, event: e.value }))}>{e.label}</button>
            ))}
          </div>
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
          <div className="settings-chips">
            <button type="button" className={"settings-chip" + (form.hook_type === "prompt" ? " on" : "")}
              onClick={() => setForm((p) => ({ ...p, hook_type: "prompt" }))}>注入提示词</button>
            <button type="button" className={"settings-chip" + (form.hook_type === "command" ? " on" : "")}
              onClick={() => setForm((p) => ({ ...p, hook_type: "command" }))}>执行命令</button>
          </div>
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
          <Sw checked={form.enabled} onChange={(v) => setForm((p) => ({ ...p, enabled: v }))} />
        </div>
      </div>
    </Modal>
  );
}
