/** 浮窗实时消息行（plan-73-340 打磨版）。
 *
 * 数据来源：主任务的会话级 WebSocket（见 petEvents.ts 的 PetSessionWs）。
 * 把高频流式增量聚合成「活动项」，供悬停浮窗按行展示：
 *   思考（thinking.delta）/ 消息（token.delta）/ 工具调用（tool.call）/ 工具结果（tool.result）。
 *
 * 展示口径（用户要求对齐主界面消息流）：
 *   · 思考与消息保留流式原文，展示层加「思考中 / 回复中」前缀；
 *   · 工具调用**不展示原始工具名**——只存工具名与入参预览，由展示层经
 *     `petToolDisplay.toolLine` 转成「动词 · 目标」（如「读取文件 · pet/PetApp.tsx」）；
 *   · 工具结果沿用它所属调用的目标（tool.result 不带入参，故记住最近一次 tool.call）。
 *
 * 节流：增量可达每秒数十条，直接 setState 会拖垮渲染；这里只写入 ref，
 * 由 150ms 的定时器合并提交一次（视觉上仍是「实时刷新」）。
 */
import { useEffect, useRef, useState } from "react";
import { petSessionWs, type PetServerEvent } from "./petEvents";

export type LiveKind = "thinking" | "message" | "tool" | "result";

export interface LiveItem {
  id: number;
  kind: LiveKind;
  /** 思考/消息：流式累积的文本；工具/结果：留空（由展示层按 tool 生成可读文案） */
  text: string;
  at: number;
  /** 工具项：工具原始名（展示层用 petToolDisplay 转成「动词 · 目标」） */
  tool?: string;
  /** 工具项：服务端下发的入参预览（超长会被截断，解析失败时降级） */
  argsPreview?: string;
  /** 仍在流式累积（展示端可加轻量指示，不改变文本） */
  streaming?: boolean;
  /** 工具结果是否成功（仅 kind=result） */
  ok?: boolean;
}

/** 保留的历史条数（只需最新一条，留几条第缓冲） */
const MAX_LIVE_ITEMS = 4;
/** 合帧间隔：高频增量的提交节奏 */
const FLUSH_MS = 150;

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** 只做首尾裁剪 —— **不截断、不压平换行**（plan-73-341）：
 *  ① 用户要求"刷新内容时不要省略"，截断会让长思考/长命令看不全；
 *  ② `LiveTicker` 依赖换行符做"上滚切行"，压平换行会让滚动视口退化成永不换行的横滚。
 *  内存风险由"只渲染最后一行 + 历史条数上限 4"控制，而非靠截断。 */
function clean(text: string): string {
  return String(text || "").replace(/^[\r\n]+/, "");
}

/**
 * 订阅主任务的实时消息流。
 *
 * @param sessionId 主任务会话（无则断开）
 * @param port      后端端口
 * @param active    是否需要连接。**plan-73-341 起语义为"常驻"**：
 *                  传「宠物窗口可见 + 存在主任务」即可，不要再挂 hover ——
 *                  流式增量不入服务端缓冲，断连即永久丢失（会表现为浮窗消息卡住）。
 */
export function useLiveActivity(sessionId: number | null, port: number | null, active: boolean) {
  const itemsRef = useRef<LiveItem[]>([]);
  const streamRef = useRef<LiveItem | null>(null);
  /** 最近一次工具调用的（工具名 + 入参预览）——tool.result 不带入参，靠它补齐目标 */
  const lastToolRef = useRef<{ tool: string; argsPreview: string } | null>(null);
  const dirtyRef = useRef(false);
  const seqRef = useRef(0);
  const [items, setItems] = useState<LiveItem[]>([]);

  useEffect(() => {
    if (!active || !sessionId || !port) {
      petSessionWs.disconnect();
      return;
    }
    const flush = () => {
      if (!dirtyRef.current) return;
      dirtyRef.current = false;
      setItems(itemsRef.current.slice());
    };
    const timer = window.setInterval(flush, FLUSH_MS);

    const pushItem = (kind: LiveKind, text: string, extra?: Partial<LiveItem>) => {
      const t = clean(text);
      // 空文本项直接丢弃（避免渲染出一行空白）；工具/结果项允许空 text（由展示层按 tool 生成）
      if (!t && !extra?.tool) return null;
      const item: LiveItem = { id: ++seqRef.current, kind, text: t, at: Date.now(), ...extra };
      itemsRef.current = [item, ...itemsRef.current].slice(0, MAX_LIVE_ITEMS);
      dirtyRef.current = true;
      return item;
    };

    /** 流式累加：同类增量在同一个活动项上继续追加 */
    const appendStream = (kind: LiveKind, delta: string) => {
      const cur = streamRef.current;
      if (cur && cur.kind === kind) {
        cur.text = clean(cur.text + delta);
        cur.at = Date.now();
        dirtyRef.current = true;
        return;
      }
      if (!clean(delta)) return; // 空增量不创建新项（避免出现空白活动行）
      streamRef.current = pushItem(kind, delta, { streaming: true });
    };

    const endStream = (kind: LiveKind) => {
      const cur = streamRef.current;
      if (cur && cur.kind === kind) {
        cur.streaming = false;
        streamRef.current = null;
        dirtyRef.current = true;
      }
    };

    const onEvent = (ev: PetServerEvent) => {
      const p = (ev.payload || {}) as Record<string, unknown>;
      switch (ev.event) {
        case "turn.started":
          // 新一轮：清空活动列表（上一轮的实时行不再具备参考价值）
          itemsRef.current = [];
          streamRef.current = null;
          lastToolRef.current = null;
          dirtyRef.current = true;
          break;
        case "thinking.delta":
          appendStream("thinking", str(p.delta));
          break;
        case "thinking.done":
          endStream("thinking");
          break;
        case "token.delta":
          appendStream("message", str(p.delta));
          break;
        case "token.done":
          endStream("message");
          break;
        case "tool.call": {
          endStream("thinking");
          endStream("message");
          const tool = str(p.tool);
          const argsPreview = str(p.args_preview);
          lastToolRef.current = { tool, argsPreview };
          pushItem("tool", "", { tool, argsPreview });
          break;
        }
        case "tool.result": {
          const tool = str(p.tool) || (lastToolRef.current && lastToolRef.current.tool) || "";
          // 复用最近一次调用的目标；工具名不一致（并发工具）时不强行套用
          const prev = lastToolRef.current;
          const argsPreview = prev && prev.tool === tool ? prev.argsPreview : "";
          pushItem("result", "", { tool, argsPreview, ok: p.ok !== false });
          break;
        }
        default:
          break;
      }
    };

    const off = petSessionWs.on(onEvent);
    petSessionWs.watch(sessionId, port);
    return () => {
      off();
      window.clearInterval(timer);
      petSessionWs.disconnect();
    };
  }, [sessionId, port, active]);

  return items;
}
