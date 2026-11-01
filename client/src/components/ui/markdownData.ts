import { Children, isValidElement, type ReactNode } from "react";

/** plan-20261101-performance：复制从数据树读取，离屏行卸载后仍能复制完整内容。 */
export function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

export function tableRows(node: ReactNode): string[][] {
  const rows: string[][] = [];
  const walk = (value: ReactNode) => Children.forEach(value, (child) => {
    if (!isValidElement<{ children?: ReactNode; node?: { tagName?: string } }>(child)) return;
    if (child.type === "tr" || child.props.node?.tagName === "tr") {
      rows.push(Children.toArray(child.props.children).filter(isValidElement).map(nodeText));
    } else walk(child.props.children);
  });
  walk(node);
  return rows;
}

function cellSource(value: ReactNode): string {
  if (!isValidElement<{ children?: ReactNode; href?: string; node?: { tagName?: string } }>(value)) {
    return Array.isArray(value) ? value.map(cellSource).join("") : nodeText(value);
  }
  const text = Children.toArray(value.props.children).map(cellSource).join("");
  const tag = typeof value.type === "string" ? value.type : value.props.node?.tagName;
  if (tag === "code") return "`" + text + "`";
  if (tag === "strong") return "**" + text + "**";
  if (tag === "em") return "*" + text + "*";
  if (tag === "del") return "~~" + text + "~~";
  if (tag === "br") return "<br>";
  if (tag === "a" && /^(https?:|mailto:)/.test(value.props.href ?? "")) return `[${text}](${value.props.href})`;
  return text;
}

export function tableMarkdown(node: ReactNode): string {
  const rows: string[][] = [];
  let align: string[] = [];
  const walk = (value: ReactNode) => Children.forEach(value, (child) => {
    if (!isValidElement<{ children?: ReactNode; node?: { tagName?: string } }>(child)) return;
    if (child.type === "tr" || child.props.node?.tagName === "tr") {
      const cells = Children.toArray(child.props.children).filter(isValidElement);
      if (!rows.length) align = cells.map((cell) => {
        const props = cell.props as { style?: { textAlign?: string } };
        return props.style?.textAlign === "center" ? ":---:" : props.style?.textAlign === "right" ? "---:" : props.style?.textAlign === "left" ? ":---" : "---";
      });
      rows.push(cells.map((cell) => cellSource(cell).replace(/\r?\n/g, " ").replace(/\|/g, "\\|").trim()));
    } else walk(child.props.children);
  });
  walk(node);
  if (!rows.length) return "";
  rows.splice(1, 0, align);
  return rows.map((row) => `| ${row.join(" | ")} |`).join("\n");
}
