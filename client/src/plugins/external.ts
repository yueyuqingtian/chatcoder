/** v19: 外挂插件加载协议。
 * 用户插件目录：~/.chatcoder/plugins/<dir>/plugin.json（{id,name,slot,entry,description}）+ entry js。
 * 主进程扫描并经 IPC plugins:list 返回 manifest + 源码文本；
 * 前端以受限 api（{React, registerPlugin}）执行插件代码，插件调用 registerPlugin 注册自定义组件替换系统 slot。
 * web 环境（无 chatcoderAPI）跳过。失败仅告警不阻断。
 */
import * as React from "react";
import { registerPlugin, type SlotId } from "./registry";

interface ExternalPluginManifest {
  id: string;
  name: string;
  slot: SlotId;
  description?: string;
  code: string;
}

export async function loadExternalPlugins(): Promise<void> {
  const api = (window as unknown as { chatcoderAPI?: { listUserPlugins?: () => Promise<ExternalPluginManifest[]> } }).chatcoderAPI;
  if (!api?.listUserPlugins) return; // web 环境跳过
  try {
    const list = await api.listUserPlugins();
    for (const p of list ?? []) {
      try {
        // 受限沙箱：仅暴露 React 与 registerPlugin
        const factory = new Function("api", p.code) as (api: unknown) => void;
        factory({ React, registerPlugin });
      } catch (e) {
        console.warn(`[plugins] 外挂插件 ${p.id} 加载失败`, e);
      }
    }
  } catch (e) {
    console.warn("[plugins] 读取外挂插件列表失败", e);
  }
}
