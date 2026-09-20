/** useTextHighlight —— 消息流搜索命中高亮（plan-282-1421 · 第11项）
 *
 * 背景：此前搜索只做「命中 entry 下标 + scrollToIndex」，渲染层没有任何标记，
 * 用户只能靠滚动位置猜，命中点看不见。
 *
 * 实现要点：
 *  - **不侵入 Markdown 结构**：在容器层遍历文本节点，用 <mark> 包裹命中片段。
 *    这样对正文、代码块、标题一视同仁，也不必改动 react-markdown 的组件映射。
 *  - **只处理关键字非空的项**：关键字为空立即还原，零开销。
 *  - **可就地还原**：每次执行前先撤销上一次的包裹（记录原始文本），避免嵌套 mark。
 *  - **当前命中项**加 is-active（配色走 tokens.css 的
 *    --color-find-highlight / --color-find-highlight-active，含深色主题值）。
 *
 * 用法：
 *   const ref = useTextHighlight<HTMLDivElement>(keyword, isActiveMatch);
 *   <div ref={ref} className="mf-list">…</div>
 */
import { useEffect, useRef } from "react";

/** 已包裹的 mark 元素 → 原始文本节点（用于还原） */
type RestoreFn = () => void;

export function useTextHighlight<T extends HTMLElement>(
  keyword: string,
  isActiveMatch: boolean,
) {
  const ref = useRef<T>(null);
  const restoreRef = useRef<RestoreFn | null>(null);

  useEffect(() => {
    const root = ref.current;
    // 先还原上一次的包裹，保证幂等
    if (restoreRef.current) {
      restoreRef.current();
      restoreRef.current = null;
    }
    if (!root) return;

    const kw = keyword.trim();
    if (!kw) return;

    const lowered = kw.toLowerCase();
    const created: HTMLElement[] = [];

    // 收集文本节点（跳过 mark 的祖先/后代判断交给还原逻辑兜底）
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const text = node.nodeValue;
        if (!text || !text.toLowerCase().includes(lowered)) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        // 不处理已包裹的、脚本样式等
        const tag = parent.tagName;
        if (tag === "SCRIPT" || tag === "STYLE") return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    const targets: Text[] = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) targets.push(n as Text);

    for (const textNode of targets) {
      const text = textNode.nodeValue ?? "";
      const lower = text.toLowerCase();
      const frag = document.createDocumentFragment();
      let cursor = 0;
      let idx = lower.indexOf(lowered);
      while (idx !== -1) {
        if (idx > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, idx)));
        const mark = document.createElement("mark");
        mark.className = "msg-hit" + (isActiveMatch ? " is-active" : "");
        mark.textContent = text.slice(idx, idx + kw.length);
        frag.appendChild(mark);
        created.push(mark);
        cursor = idx + kw.length;
        idx = lower.indexOf(lowered, cursor);
      }
      if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
      textNode.parentNode?.replaceChild(frag, textNode);
    }

    restoreRef.current = () => {
      for (const mark of created) {
        const parent = mark.parentNode;
        if (!parent) continue;
        parent.replaceChild(document.createTextNode(mark.textContent ?? ""), mark);
        // 合并相邻文本节点，避免 DOM 碎片累积
        parent.normalize();
      }
    };

    return () => {
      // 卸载前还原，避免遗留半包裹状态
      if (restoreRef.current) {
        restoreRef.current();
        restoreRef.current = null;
      }
    };
  }, [keyword, isActiveMatch]);

  return ref;
}
