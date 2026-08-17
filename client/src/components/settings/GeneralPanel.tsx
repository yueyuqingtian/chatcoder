/** 设置中心：常规（v2.2 对齐 zcode 3.18）。
 * 界面语言、HTTP 代理、终端 Shell/字体、增强搜索、消息流显示开关。
 * 所有设置项走 /settings/global 持久化（config.json），重启不丢。 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useUiStore } from "../../store/ui";
import { Row, Sw } from "./shared";

const TERMINAL_SHELLS = [
  { value: "auto", label: "自动（按平台默认）" },
  { value: "pwsh", label: "PowerShell 7 (pwsh)" },
  { value: "powershell", label: "Windows PowerShell" },
  { value: "cmd", label: "命令提示符 (cmd)" },
  { value: "git-bash", label: "Git Bash" },
];

export function GeneralPanel() {
  const ui = useUiStore();
  const [cfg, setCfg] = useState({
    terminal_shell: "auto", terminal_font: "", http_proxy: "",
    enhanced_search: true, show_reasoning: true,
    auto_approve_tools: false, force_approval_tools: "",
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        terminal_shell: g.terminal_shell || "auto",
        terminal_font: g.terminal_font || "",
        http_proxy: g.http_proxy || "",
        enhanced_search: g.enhanced_search,
        show_reasoning: g.show_reasoning,
        auto_approve_tools: g.auto_approve_tools,
        force_approval_tools: g.force_approval_tools || "",
      });
    } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  const patch = (p: Partial<typeof cfg>) => setCfg((prev) => ({ ...prev, ...p }));

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.setGlobalSettings({
        terminal_shell: cfg.terminal_shell,
        terminal_font: cfg.terminal_font,
        http_proxy: cfg.http_proxy,
        enhanced_search: cfg.enhanced_search,
        show_reasoning: cfg.show_reasoning,
        auto_approve_tools: cfg.auto_approve_tools,
        force_approval_tools: cfg.force_approval_tools,
      });
      // v1.1: 保存即生效——刷新 todos/reasoning 显示开关
      await useUiStore.getState().refreshGlobalFlags();
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (e) { alert("保存失败: " + String(e)); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <div className="settings-card">
        <Row title="界面语言" desc="中文 / English">
          <select className="ui-select" value={ui.language} onChange={(e) => ui.setLanguage(e.target.value as "zh" | "en")}>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </Row>
        <Row title="HTTP 代理" desc="全局 HTTP/HTTPS 代理（写入后端环境变量，立即生效）">
          <input className="ui-input" placeholder="如 http://127.0.0.1:7890" value={cfg.http_proxy} onChange={(e) => patch({ http_proxy: e.target.value })} />
        </Row>
        <Row title="集成终端 Shell" desc="新终端标签使用的 Shell（重启终端生效）">
          <select className="ui-select" value={cfg.terminal_shell} onChange={(e) => patch({ terminal_shell: e.target.value })}>
            {TERMINAL_SHELLS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </Row>
        <Row title="终端字体" desc="留空自动继承系统终端字体">
          <input className="ui-input" placeholder="如 Cascadia Code, monospace" value={cfg.terminal_font} onChange={(e) => patch({ terminal_font: e.target.value })} />
        </Row>
      </div>

      <div className="settings-card">
        <Row title="增强搜索（ripgrep）" desc="使用 ripgrep 进行更快的全库文本搜索">
          <Sw checked={cfg.enhanced_search} onChange={(v) => patch({ enhanced_search: v })} />
        </Row>
        <Row title="消息流显示 reasoning" desc="在消息流中渲染思考过程块（ThinkingBlock）">
          <Sw checked={cfg.show_reasoning} onChange={(v) => patch({ show_reasoning: v })} />
        </Row>
        <Row title="自动批准工具调用" desc="开启后自动允许工具请求；关闭时工作区外访问会弹出手动审批">
          <Sw checked={cfg.auto_approve_tools} onChange={(v) => patch({ auto_approve_tools: v })} />
        </Row>
        <Row title="始终需要审批的工具" desc="用逗号分隔工具名，例如 read_file, write_file">
          <input className="ui-input" placeholder="留空表示不额外强制审批" value={cfg.force_approval_tools} onChange={(e) => patch({ force_approval_tools: e.target.value })} />
        </Row>
        <Row title="消息流密度" desc="思考/工具调用/文本等消息块之间的行间距（立即生效）">
          <select
            className="ui-select"
            value={ui.msgDensity}
            onChange={(e) => ui.setPrefs({ msgDensity: e.target.value as "comfortable" | "compact" })}
          >
            <option value="comfortable">舒适</option>
            <option value="compact">紧凑</option>
          </select>
        </Row>
      </div>

      <div className="settings-create-actions" style={{ marginTop: 12 }}>
        <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>
          {saving ? "保存中…" : saved ? "已保存 ✓" : "保存设置"}
        </button>
      </div>
    </div>
  );
}
