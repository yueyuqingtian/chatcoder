/** callout —— GFM 语义提示块（[!NOTE] / [!TIP] / [!IMPORTANT] / [!WARNING] / [!CAUTION]）纯解析逻辑。
 *
 *  plan-seamless-flow-ui：让消息流中的提示、建议与风险说明具备清晰视觉层次，
 *  渲染层以圆角柔色语义面 + 图标标题表达，不使用左侧装饰竖线。
 */
import { Children, cloneElement, isValidElement, type ReactNode } from "react";

export type CalloutKind = "note" | "tip" | "important" | "warning" | "caution";
export type CalloutTone = "info" | "tip" | "important" | "warn" | "danger";

export interface CalloutMeta {
  kind: CalloutKind;
  tone: CalloutTone;
  label: string;
}

export interface ParsedCallout {
  kind: CalloutKind;
  tone: CalloutTone;
  title: string;
  body: ReactNode[];
}

const CALLOUT_MAP: Record<string, CalloutMeta> = {
  NOTE: { kind: "note", tone: "info", label: "提示" },
  TIP: { kind: "tip", tone: "tip", label: "建议" },
  IMPORTANT: { kind: "important", tone: "important", label: "重要" },
  WARNING: { kind: "warning", tone: "warn", label: "注意" },
  CAUTION: { kind: "caution", tone: "danger", label: "警示" },
};

const MARKER_RE = /^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](?:[ \t]+([^\r\n]*))?(?:\r?\n|$)/i;

function isBlankNode(node: ReactNode): boolean {
  return typeof node === "string" && node.trim() === "";
}

/** 识别 blockquote 子树是否以 GFM Alert 标记起头；命中则返回语义类型、标题与剥离标记后的正文节点。 */
export function parseCalloutChildren(children: ReactNode): ParsedCallout | null {
  const list = Children.toArray(children);
  const firstIdx = list.findIndex((n) => !isBlankNode(n));
  if (firstIdx < 0) return null;

  const firstEl = list[firstIdx];
  if (!isValidElement<{ children?: ReactNode }>(firstEl)) return null;

  const pChildren = Children.toArray(firstEl.props.children);
  const firstTextIdx = pChildren.findIndex((n) => !isBlankNode(n));
  if (firstTextIdx < 0) return null;

  const leading = pChildren[firstTextIdx];
  if (typeof leading !== "string") return null;

  const m = MARKER_RE.exec(leading);
  if (!m) return null;

  const meta = CALLOUT_MAP[m[1].toUpperCase()];
  if (!meta) return null;

  const inlineTitle = (m[2] ?? "").trim();
  const restOfLeading = leading.slice(m[0].length);
  const trailingPChildren = pChildren.slice(firstTextIdx + 1);
  const restBlocks = list.slice(firstIdx + 1).filter((n) => !isBlankNode(n));

  const hasMoreAfterFirstLine = restOfLeading.trim().length > 0 || trailingPChildren.length > 0 || restBlocks.length > 0;

  // 若整块只有单行「> [!TIP] 某句提示」，把该句作为正文、保留默认语义标题，避免正文区为空
  if (!hasMoreAfterFirstLine && inlineTitle) {
    const nextFirst = cloneElement(firstEl, undefined, inlineTitle);
    return {
      kind: meta.kind,
      tone: meta.tone,
      title: meta.label,
      body: [nextFirst],
    };
  }

  const title = inlineTitle ? `${meta.label} · ${inlineTitle}` : meta.label;
  const nextPChildren: ReactNode[] = [];
  if (restOfLeading.length > 0) nextPChildren.push(restOfLeading);
  nextPChildren.push(...trailingPChildren);

  const body: ReactNode[] = [];
  if (nextPChildren.some((n) => !isBlankNode(n))) {
    body.push(cloneElement(firstEl, undefined, ...nextPChildren));
  }
  body.push(...restBlocks);

  return {
    kind: meta.kind,
    tone: meta.tone,
    title,
    body,
  };
}
