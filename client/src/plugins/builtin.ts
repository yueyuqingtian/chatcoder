/** v19: 内置插件注册——系统组件全部以 slot 插件形式登记，
 * 用户可在设置页「插件」中替换任一 slot 的生效组件（拼积木式）。 */
import { registerPlugin, applyStoredOverrides } from "./registry";
import { Sidebar } from "../components/Sidebar";
import { SettingsSidebar } from "../components/settings/SidebarShell";
import { TitleBar } from "../components/TitleBar";
import { MessageFlow } from "../components/chat/MessageFlow";
import { ComposerBox } from "../components/chat/ComposerBox";
import { EmptyState } from "../components/Workspace";
import { RightPanel } from "../components/panel/RightPanel";
import { ThinkingBlock } from "../components/chat/ThinkingBlock";
import { ToolTree } from "../components/chat/ToolTree";
import { SubagentCard } from "../components/chat/SubagentCard";

let _registered = false;

export function registerBuiltinPlugins(): void {
  if (_registered) return;
  _registered = true;
  registerPlugin({ id: "builtin.sidebar", name: "会话侧栏", slot: "sidebar", description: "左侧项目/会话导航栏", builtin: true, replaceable: true, component: Sidebar });
  registerPlugin({ id: "builtin.settings-sidebar", name: "设置侧栏", slot: "settings-sidebar", description: "设置页左侧分组导航", builtin: true, replaceable: true, component: SettingsSidebar });
  registerPlugin({ id: "builtin.titlebar", name: "顶部标题栏", slot: "titlebar", description: "会话标题 + 窗口控制 + 终端/面板开关", builtin: true, replaceable: true, component: TitleBar });
  registerPlugin({ id: "builtin.message-flow", name: "消息流", slot: "message-flow", description: "消息流时间线（主会话/子代理共用，支持数据源注入 source=main|subagent + threadId）", builtin: true, replaceable: true, component: MessageFlow, props: { source: "main" } });
  registerPlugin({ id: "builtin.composer", name: "对话输入框", slot: "composer", description: "消息页输入框（ComposerCore chat 变体）", builtin: true, replaceable: true, component: ComposerBox });
  registerPlugin({ id: "builtin.empty-state", name: "空态首页", slot: "empty-state", description: "问候语 + 首页输入框（ComposerCore home 变体）", builtin: true, replaceable: true, component: EmptyState });
  registerPlugin({ id: "builtin.right-panel", name: "右侧面板", slot: "right-panel", description: "任务摘要/浏览器/终端/文件/子代理标签页", builtin: true, replaceable: true, component: RightPanel });
  registerPlugin({ id: "builtin.thinking-block", name: "思考块", slot: "thinking-block", description: "思考过程折叠块", builtin: true, replaceable: true, component: ThinkingBlock });
  registerPlugin({ id: "builtin.tool-tree", name: "工具调用树", slot: "tool-tree", description: "工具调用行（合并行/写操作行）", builtin: true, replaceable: true, component: ToolTree });
  registerPlugin({ id: "builtin.subagent-card", name: "子代理卡片", slot: "subagent-card", description: "消息流子代理入口卡片", builtin: true, replaceable: true, component: SubagentCard });
  applyStoredOverrides();
}
