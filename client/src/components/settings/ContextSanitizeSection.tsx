/** 设置中心：上下文脱敏编辑卡片（plan-20261124-context-sanitize）。
 *
 * 用户视角操作路径：
 *  1. 顶部开关一键启停上下文构造脱敏（修改即保存）；
 *  2. 开关开启时通过 ui/Collapse 平滑展开全部关键词（流式换行平铺，不折叠藏在 +N 浮层里，
 *     第一眼即可看清默认词表）；
 *  3. 支持：输入回车或点击添加单个词、逗号/分号/换行批量粘贴、点击标签文字原地修改、
 *     点击 × 一键删除、改动后右上角显式提供「恢复默认」一键找回内置词表。
 */
import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { IconX } from "../icons";
import { Button, CardRow, Collapse, Input, Section, Switch, Tooltip } from "../ui";

/** 兜底默认词表（后端未返回或首帧加载前使用；加载完成后以服务端返回为唯一事实源） */
export const FALLBACK_DEFAULT_SANITIZE_KEYWORDS: string[] = [
  "zcode",
  "deepseek harness",
  "qoder",
  "workbuddy",
  "codebuddy",
  "claude code",
  "codex",
  "opencode",
  "pi agent",
  "pi",
  "deepseek",
  "glm",
  "kimi",
  "xiaomi",
];

const MAX_KEYWORD_LEN = 80;
const MAX_KEYWORDS = 200;
const SPLIT_RE = /[,，;；\r\n]+/;

function cleanKeyword(raw: string): string {
  return raw.replace(/\u200b/g, "").replace(/[ \t]+/g, " ").trim();
}

export function isSameKeywordList(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}

interface ContextSanitizeSectionProps {
  enabled: boolean;
  keywords: string[];
  defaultKeywords: string[];
  onEnabledChange: (enabled: boolean) => void;
  onKeywordsChange: (keywords: string[]) => void;
}

export function ContextSanitizeSection({
  enabled,
  keywords,
  defaultKeywords,
  onEnabledChange,
  onKeywordsChange,
}: ContextSanitizeSectionProps) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const editInputRef = useRef<HTMLInputElement>(null);

  const isDefault = useMemo(
    () => isSameKeywordList(keywords, defaultKeywords),
    [keywords, defaultKeywords],
  );

  useEffect(() => {
    if (editingIndex != null) {
      editInputRef.current?.focus();
      editInputRef.current?.select();
    }
  }, [editingIndex]);

  // 列表长度变化越界时自动退出编辑态
  useEffect(() => {
    if (editingIndex != null && editingIndex >= keywords.length) {
      setEditingIndex(null);
      setEditingValue("");
    }
  }, [editingIndex, keywords.length]);

  const addRawText = (raw: string) => {
    const pieces = raw
      .split(SPLIT_RE)
      .map(cleanKeyword)
      .filter(Boolean);
    if (pieces.length === 0) return;

    const existing = new Set(keywords.map((k) => k.toLowerCase()));
    const next = [...keywords];
    let added = 0;
    let dupCount = 0;
    let tooLong = false;
    let hitLimit = false;

    for (const item of pieces) {
      if (item.length > MAX_KEYWORD_LEN) {
        tooLong = true;
        continue;
      }
      const key = item.toLowerCase();
      if (existing.has(key)) {
        dupCount += 1;
        continue;
      }
      if (next.length >= MAX_KEYWORDS) {
        hitLimit = true;
        break;
      }
      existing.add(key);
      next.push(item);
      added += 1;
    }

    if (added > 0) {
      onKeywordsChange(next);
      setDraft("");
    }

    if (hitLimit) {
      setError(`最多保留 ${MAX_KEYWORDS} 个脱敏关键词`);
    } else if (tooLong) {
      setError(`单个关键词不超过 ${MAX_KEYWORD_LEN} 个字符`);
    } else if (added === 0 && dupCount > 0) {
      setError(dupCount === 1 ? "该关键词已存在（匹配时不区分大小写）" : "输入的关键词均已存在");
    } else {
      setError(null);
    }
  };

  const handleAddKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addRawText(draft);
    }
  };

  const handlePaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData("text");
    if (text && SPLIT_RE.test(text)) {
      e.preventDefault();
      addRawText(draft ? `${draft},${text}` : text);
    }
  };

  const startEdit = (index: number) => {
    setEditingIndex(index);
    setEditingValue(keywords[index] ?? "");
    setError(null);
  };

  const cancelEdit = () => {
    setEditingIndex(null);
    setEditingValue("");
    setError(null);
  };

  const commitEdit = () => {
    if (editingIndex == null) return;
    const cleaned = cleanKeyword(editingValue);
    if (!cleaned) {
      // 清空即视为取消本次编辑，避免误删；要删除请点右侧 ×
      cancelEdit();
      return;
    }
    if (cleaned.length > MAX_KEYWORD_LEN) {
      setError(`单个关键词不超过 ${MAX_KEYWORD_LEN} 个字符`);
      return;
    }
    const lower = cleaned.toLowerCase();
    const duplicated = keywords.some((kw, idx) => idx !== editingIndex && kw.toLowerCase() === lower);
    if (duplicated) {
      setError("已存在同名关键词（不区分大小写）");
      return;
    }
    if (cleaned !== keywords[editingIndex]) {
      const next = keywords.map((kw, idx) => (idx === editingIndex ? cleaned : kw));
      onKeywordsChange(next);
    }
    setEditingIndex(null);
    setEditingValue("");
    setError(null);
  };

  const removeAt = (index: number) => {
    if (editingIndex === index) {
      setEditingIndex(null);
      setEditingValue("");
    } else if (editingIndex != null && editingIndex > index) {
      setEditingIndex(editingIndex - 1);
    }
    onKeywordsChange(keywords.filter((_, idx) => idx !== index));
    setError(null);
  };

  const handleReset = () => {
    setEditingIndex(null);
    setEditingValue("");
    setDraft("");
    setError(null);
    onKeywordsChange([...defaultKeywords]);
  };

  return (
    <Section title="上下文脱敏">
      <CardRow
        className={`gp-sanitize-switch-row${enabled ? "" : " is-collapsed"}`}
        title="发送前自动脱敏关键词"
        desc="构造模型上下文时，在命中关键词（不区分大小写）左右各自插入零宽字符；仅作用于出站请求，不影响本地记录与工具执行。"
      >
        <Switch checked={enabled} onChange={onEnabledChange} aria-label="发送前自动脱敏关键词" />
      </CardRow>

      <Collapse open={enabled}>
        <div className="gp-sanitize-body">
          <div className="gp-sanitize-head">
            <div className="ui-card-row-info">
              <div className="ui-card-row-title gp-sanitize-title">
                <span>脱敏关键词</span>
                <span className="gp-sanitize-count">{keywords.length}</span>
              </div>
              <div className="ui-card-row-desc">
                点击标签可原地修改，支持逗号或换行批量粘贴。
              </div>
            </div>
            {!isDefault && (
              <div className="ui-card-row-control">
                <Button variant="secondary" size="sm" onClick={handleReset}>
                  恢复默认
                </Button>
              </div>
            )}
          </div>

          {keywords.length > 0 ? (
            <div className="gp-sanitize-tags" role="list" aria-label="脱敏关键词列表">
              {keywords.map((kw, idx) => {
                if (editingIndex === idx) {
                  return (
                    <span key={`edit-${idx}`} className="gp-sanitize-edit-wrap" role="listitem">
                      <input
                        ref={editInputRef}
                        className="gp-sanitize-edit-input"
                        value={editingValue}
                        style={{ width: `${Math.min(28, Math.max(6, editingValue.length + 2))}ch` }}
                        aria-label={`编辑关键词 ${kw}`}
                        onChange={(e) => {
                          setEditingValue(e.target.value);
                          if (error) setError(null);
                        }}
                        onBlur={commitEdit}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            commitEdit();
                          } else if (e.key === "Escape") {
                            e.preventDefault();
                            cancelEdit();
                          }
                        }}
                      />
                    </span>
                  );
                }
                return (
                  <span
                    key={`${kw.toLowerCase()}-${idx}`}
                    className="gp-sanitize-chip"
                    role="listitem"
                  >
                    <Tooltip content="点击修改">
                      <span
                        role="button"
                        tabIndex={0}
                        className="gp-sanitize-chip-text"
                        onClick={() => startEdit(idx)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            startEdit(idx);
                          }
                        }}
                      >
                        {kw}
                      </span>
                    </Tooltip>
                    <Tooltip content="移除">
                      <button
                        type="button"
                        className="ui-taginput-x"
                        aria-label={`移除 ${kw}`}
                        onClick={() => removeAt(idx)}
                      >
                        <IconX size={10} />
                      </button>
                    </Tooltip>
                  </span>
                );
              })}
            </div>
          ) : (
            <div className="gp-sanitize-empty">
              当前无脱敏关键词，发送上下文时不会插入零宽字符；可在下方添加或点击「恢复默认」。
            </div>
          )}

          <div className="gp-sanitize-add">
            <Input
              value={draft}
              invalid={Boolean(error)}
              placeholder="输入关键词，回车添加（如 cursor、claude code）"
              aria-label="新增脱敏关键词"
              onChange={(e) => {
                setDraft(e.target.value);
                if (error) setError(null);
              }}
              onKeyDown={handleAddKeyDown}
              onPaste={handlePaste}
            />
            <Button
              variant="secondary"
              size="md"
              disabled={!cleanKeyword(draft)}
              onClick={() => addRawText(draft)}
            >
              添加
            </Button>
          </div>

          {error && (
            <div className="gp-sanitize-error" role="alert">
              {error}
            </div>
          )}
        </div>
      </Collapse>
    </Section>
  );
}
