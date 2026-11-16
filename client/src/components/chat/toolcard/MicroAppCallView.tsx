/** MicroAppCallView（plan-693-2868 P1）：`microapp_call` 工具调用的卡片体。
 *
 * 为什么单独成组件：ToolTree 的展开区里其它结构化呈现（grep 命中、db_* 结果、问答详情）都是
 * 就地小段 JSX，而微应用调用需要「回显它调了哪个应用的哪个方法 + 带出参数 + 一个去界面的入口」，
 * 放在 ToolTree 里会把那条已经很长的渲染分支再撑开一层（见 ToolTree.tsx 的 LeafRow）。
 *
 * 三条产品约束（plan-693-2868 §7 P1 / §6.6）：
 *  ① 卡片自己**不**打开面板：AI 调方法不等于用户要看界面（发送同样不打开）；
 *     只有用户点「查看界面」才 openMicroApp；同一 appId 由 store 去重聚焦已有标签。
 *  ② appId 与 method 必须直接可见：用户要能一眼判断「AI 刚才动的是哪个应用、哪个方法」。
 *  ③ 参数只在存在时回显，空参数不铺一块 `{}`。
 *
 * 参数口径跟随后端 P0 的工具面（server/app/orchestration/tools/microapp.py）：
 * `microapp_call` 的入参是 `{ appId, method, args }`——`args` 才是传给方法的业务参数，
 * 方法返回体里也回显同一份 `args`。早期讨论稿把它写作 `data`，这里两者都兼容。
 */
import { memo, useMemo } from "react";
import { Button, Tooltip } from "../../ui";
import { ClampBlock } from "../ClampBlock";
import { usePanelStore } from "../../../store/panel";
import { useMicroAppsStore } from "../../../store/microapps";
import { notify } from "../../../store/toast";

export interface MicroAppCallViewProps {
  /** 工具调用参数（microapp_call 的 args） */
  args: Record<string, unknown>;
  /** 工具结果原文（可能为空：运行中还没有返回） */
  output?: string | null;
  /** 调用状态：null=运行中（用于按钮/空态文案） */
  ok: boolean | null;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

export const MicroAppCallView = memo(function MicroAppCallView({ args, output, ok }: MicroAppCallViewProps) {
  // 参数名兼容 appId / app_id 两种写法（清单定稿是 appId，模型偶发会写成蛇形）
  const appId = str(args.appId) || str(args.app_id);
  const method = str(args.method);

  /** 传给方法的业务参数：args（后端定稿）/ data（早期讨论稿）/ params */
  const payloadText = useMemo(() => {
    const payload = args.args ?? args.data ?? args.params;
    if (payload == null) return "";
    if (typeof payload === "string") return payload;
    try {
      return JSON.stringify(payload, null, 2) ?? "";
    } catch {
      return String(payload);
    }
  }, [args]);

  /** 只在用户显式点击时打开面板。
   *  工具参数里**没有 version**（只有 appId + method），而面板标签要求 version 非空才渲染，
   *  所以这里用已安装清单（store/microapps.ts，与 / 菜单同一份）补齐 version / entry；
   *  补不到就明确提示，而不是打开一个「没有绑定微应用」的空标签。 */
  const openInterface = async () => {
    if (!appId) return;
    const store = useMicroAppsStore.getState();
    let install = store.resolve(appId);
    if (!install) {
      await store.load(true);
      install = useMicroAppsStore.getState().resolve(appId);
    }
    if (!install?.version) {
      notify.error(`找不到微应用「${appId}」的安装信息，无法打开界面`);
      return;
    }
    usePanelStore.getState().openMicroApp(appId, install.version, install.entry ?? undefined);
  };

  return (
    <div className="tc-microapp">
      <div className="tc-microapp-head">
        <span className="tc-microapp-app">{appId || "（缺少 appId）"}</span>
        {method && <span className="tc-microapp-method">{method}</span>}
      </div>
      {payloadText
        ? <ClampBlock text={payloadText} preClassName="tc-plain" copyText={payloadText} />
        : (ok === null ? <span className="tc-plain tc-live">正在调用微应用方法…</span> : null)}
      {output && <ClampBlock text={output} preClassName="tc-plain" copyText={output} />}
      <div className="tc-clamp-foot">
        <Tooltip content={appId ? "在右侧面板打开这个微应用（同一个应用复用已有标签）" : "这条记录没有 appId，无法定位到界面"}>
          <Button size="xs" variant="subtle" disabled={!appId} onClick={() => void openInterface()}>查看界面</Button>
        </Tooltip>
      </div>
    </div>
  );
});
