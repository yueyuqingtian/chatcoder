/** TagInput —— 标签式输入：回车把当前输入提交为一个标签，标签可单独删除。
 *
 * 布局约定（plan-20261023-taginput-overflow）：始终单行、不换行、不出现横向滚动条。
 * 空间不足时按实际可用宽度折叠尾部标签，末尾显示「+N」；点开浮层可查看并删除被折叠项。
 * 输入位始终保留最小宽度——否则折叠后用户无法继续添加。
 *
 * 用于「个数即数量」的配置项（如异常重试：每个标签是一次重试的等待秒数，标签个数即重试次数）。
 * 校验由调用方给出：validate 返回错误文案则拒绝提交，并在下方提示，输入内容保留不丢。
 */
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { IconX } from "../icons";
import { Badge } from "./Badge";
import { Popover } from "./Popover";

/** 输入位最小宽度：折叠时优先保住它，保证「新增标签」这条主路径始终可用 */
const MIN_INPUT_W = 72;
/** 标签间距兜底值，与 .ui-taginput 的 gap 一致（正常从计算样式读取） */
const TAG_GAP = 6;

export interface TagInputProps {
  values: string[];
  onChange: (values: string[]) => void;
  /** 校验单个输入：返回错误文案则拒绝提交；返回 null 表示通过 */
  validate?: (raw: string) => string | null;
  /** 最多可添加的标签数（达到后不再接受新输入） */
  maxTags?: number;
  placeholder?: string;
  /** 标签的展示文案，如 (v) => `${v} 秒` */
  formatTag?: (value: string) => string;
  /** 整体宽度（px）：放不下的标签折叠为「+N」，不再横向滚动 */
  width?: number;
  "aria-label"?: string;
}

export function TagInput({
  values,
  onChange,
  validate,
  maxTags,
  placeholder,
  formatTag,
  width = 220,
  ...rest
}: TagInputProps) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  /** 当前能完整显示的标签个数；其余折叠为「+N」 */
  const [visibleCount, setVisibleCount] = useState(values.length);
  const [openOverflow, setOpenOverflow] = useState(false);
  const full = maxTags != null && values.length >= maxTags;
  const hidden = values.slice(visibleCount);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const overflowRef = useRef<HTMLButtonElement>(null);

  // 折叠计算：用隐藏测量层拿到每个标签的真实宽度，再从「全部显示」往下找
  // 第一个能同时容纳「可见标签 + 末尾 +N + 输入位」的组合。布局效应保证首帧即定稿，无闪烁。
  useLayoutEffect(() => {
    const box = boxRef.current;
    const measure = measureRef.current;
    if (!box || !measure) return;
    let alive = true;
    const recalc = () => {
      if (!alive) return;
      // 容器尚未布局（隐藏 tab / 弹窗未展开）时不要算——否则会把可见数压成 0
      if (box.clientWidth === 0) return;
      const cs = getComputedStyle(box);
      const padL = parseFloat(cs.paddingLeft || "0") || 0;
      const padR = parseFloat(cs.paddingRight || "0") || 0;
      const gap = parseFloat(cs.columnGap || "") || TAG_GAP;
      const avail = box.clientWidth - padL - padR;
      const tagEls = Array.from(measure.querySelectorAll<HTMLElement>("[data-measure-tag]"));
      const widths = tagEls.map((el) => el.offsetWidth);
      if (widths.length === 0) {
        setVisibleCount(0);
        return;
      }
      // +N 按「最宽位数」测量（样本渲染的是当前总数的徽标），避免少放一个/溢出
      const badge = measure.querySelector<HTMLElement>("[data-measure-badge]");
      const badgeW = badge?.offsetWidth || 34;
      const total = widths.reduce((a, b) => a + b, 0);
      // 留 1px 安全余量：亚像素取整后可能差半像素，宁可少显示一个也不要溢出裁切
      const all = total + (widths.length - 1) * gap + gap + MIN_INPUT_W + 1;
      if (all <= avail) {
        setVisibleCount(widths.length);
        return;
      }
      let acc = 0;
      let fits = 0;
      for (let i = 0; i < widths.length; i += 1) {
        const next = acc + (i === 0 ? 0 : gap) + widths[i];
        if (next + gap + badgeW + gap + MIN_INPUT_W + 1 <= avail) {
          acc = next;
          fits = i + 1;
        } else {
          break;
        }
      }
      setVisibleCount(fits);
    };
    recalc();
    const ro = new ResizeObserver(recalc);
    ro.observe(box);
    // 同时观察测量层：字体/样式晚于首帧生效时，标签真实宽度会变（如 11.5px → 13px），
    // 只盯 box 不会触发重算，折叠个数会一直停在首帧的偏大值
    ro.observe(measure);
    // 字体异步就绪后再补测一次（document.fonts 变化不一定改变测量层盒宽）
    void document.fonts?.ready.then(recalc);
    return () => {
      alive = false;
      ro.disconnect();
    };
  }, [values]);

  // 折叠项清空后浮层失去挂载理由，自动收起
  useEffect(() => {
    if (openOverflow && hidden.length === 0) setOpenOverflow(false);
  }, [openOverflow, hidden.length]);

  const commit = () => {
    const text = draft.trim();
    if (!text) return;
    if (full) {
      setError(`最多添加 ${maxTags} 个`);
      return;
    }
    const message = validate ? validate(text) : null;
    if (message) {
      setError(message);
      return;
    }
    onChange([...values, text]);
    setDraft("");
    setError(null);
  };

  const removeAt = (index: number) => {
    onChange(values.filter((_, i) => i !== index));
    setError(null);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !draft && values.length > 0) {
      removeAt(values.length - 1);
    }
  };

  return (
    <div className="ui-taginput-wrap" style={{ width }}>
      <div
        ref={boxRef}
        className="ui-input ui-taginput"
        aria-invalid={error ? true : undefined}
        onClick={() => inputRef.current?.focus()}
      >
        {values.slice(0, visibleCount).map((v, i) => (
          <Badge key={`${i}-${v}`} size="sm" tone="neutral" variant="outline" className="ui-taginput-tag">
            {formatTag ? formatTag(v) : v}
            <button
              type="button"
              className="ui-taginput-x"
              aria-label={`移除 ${formatTag ? formatTag(v) : v}`}
              onClick={() => removeAt(i)}
            >
              <IconX size={10} />
            </button>
          </Badge>
        ))}
        {hidden.length > 0 && (
          <button
            ref={overflowRef}
            type="button"
            className="ui-taginput-overflow"
            aria-label={`还有 ${hidden.length} 个标签，点击查看`}
            aria-haspopup="dialog"
            aria-expanded={openOverflow}
            onClick={(e) => {
              e.stopPropagation();
              setOpenOverflow((o) => !o);
            }}
          >
            +{hidden.length}
          </button>
        )}
        <input
          ref={inputRef}
          className="ui-taginput-field"
          value={draft}
          placeholder={
            full ? `已达上限 ${maxTags} 个` : values.length > 0 ? undefined : placeholder
          }
          disabled={full}
          aria-label={rest["aria-label"]}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError(null);
          }}
          onKeyDown={onKeyDown}
        />
      </div>

      {/* 被折叠的标签：浮层内可逐项删除，删除后可见区会自动补位 */}
      <Popover
        open={openOverflow}
        onClose={() => setOpenOverflow(false)}
        placement="bottom-end"
        ignoreRefs={[overflowRef]}
        className="ui-taginput-pop"
        aria-label="被折叠的标签"
      >
        <div className="ui-taginput-pop-title">另有 {hidden.length} 个标签</div>
        <div className="ui-taginput-pop-list">
          {hidden.map((v, i) => {
            const label = formatTag ? formatTag(v) : v;
            return (
              <div className="ui-taginput-pop-row" key={`hidden-${i}-${v}`}>
                <span className="ui-taginput-pop-label">{label}</span>
                <button
                  type="button"
                  className="ui-taginput-x"
                  aria-label={`移除 ${label}`}
                  onClick={() => removeAt(visibleCount + i)}
                >
                  <IconX size={10} />
                </button>
              </div>
            );
          })}
        </div>
      </Popover>

      {/* 测量层：真实样式渲染全部标签与 +N 样本，仅供量宽；width:0 的外壳保证不影响布局与页面滚动 */}
      <div className="ui-taginput-measure" ref={measureRef} aria-hidden="true">
        <div className="ui-taginput-measure-row">
          {values.map((v, i) => (
            <Badge
              key={`measure-${i}-${v}`}
              size="sm"
              tone="neutral"
              variant="outline"
              className="ui-taginput-tag"
              data-measure-tag
            >
              {formatTag ? formatTag(v) : v}
              <span className="ui-taginput-x">
                <IconX size={10} />
              </span>
            </Badge>
          ))}
          <span className="ui-taginput-overflow" data-measure-badge>
            +{values.length}
          </span>
        </div>
      </div>

      {error && <div className="ui-taginput-error" role="alert">{error}</div>}
    </div>
  );
}
