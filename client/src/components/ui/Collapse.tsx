/** Collapse —— 统一折叠容器（plan-282-1416）
 *
 * 用 grid-template-rows 0fr↔1fr 做真实高度过渡（见方案 §3.6），
 * 取代此前的 max-height 估算与「展开/折叠直接换图标」硬切。
 * 默认内容始终挂载（保留内部状态），折叠态不播放内部动画。
 *
 * plan-20261019-motion-sweep：补齐「`{open && <X/>}` 条件渲染 → 瞬间出现 / 消失」老写法所需的能力——
 *  · lazy：折叠过渡结束后才卸载内容、展开时先挂载再过渡（长列表 / 懒加载场景，如左栏项目下的会话行）。
 *    children 可传函数：折叠且已卸载时不会被调用，不为不可见内容白白构造元素；
 *    收起过渡期间内容冻结为最后一次展开态的快照，动画中途不会因数据被清空而闪成别的内容；
 *  · appear：首次挂载从折叠态起步、下一帧再展开（分页追加的一批行、新出现的提示条入场）；
 *  · 折叠态内部加 inert：隐藏内容不再可被聚焦，也不会被读屏读到。
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { motionDisabled } from "./usePresence";

interface InertProps {
  /** React 18 的 DOM 类型未内置 inert，用字符串透传 */
  inert?: string;
}

interface CollapseProps {
  open: boolean;
  children: ReactNode | (() => ReactNode);
  className?: string;
  /** 收起过渡结束后卸载内容（默认始终挂载） */
  lazy?: boolean;
  /** 首次挂载也播放展开过渡（默认首帧即为 open 的最终态） */
  appear?: boolean;
}

export function Collapse({ open, children, className = "", lazy = false, appear = false }: CollapseProps) {
  const ref = useRef<HTMLDivElement>(null);
  /** appear：首帧以折叠态渲染，布局副作用里强制一次样式计算后再放开，过渡才有起点 */
  const [armed, setArmed] = useState(!appear);
  /** lazy：内容是否挂载（展开即挂载，收起过渡结束才卸载） */
  const [mounted, setMounted] = useState(open);
  /** lazy：最后一次展开态的内容快照，收起过渡期间原样渲染 */
  const frozen = useRef<ReactNode>(null);

  // 派生状态：展开时同一次渲染里就挂上内容，高度过渡从 0 开始而不是先空转再突然撑开
  // （只有 lazy 才需要——非 lazy 的容器内容始终挂载，不为它多触发一次渲染）
  if (lazy && open && !mounted) setMounted(true);

  useLayoutEffect(() => {
    if (armed) return;
    void ref.current?.offsetHeight;
    setArmed(true);
  }, [armed]);

  // 兜底卸载：transitionend 在「动画效果：关闭」档或元素被隐藏时可能不触发
  useEffect(() => {
    if (!lazy || open || !mounted) return;
    const finish = () => {
      frozen.current = null;
      setMounted(false);
    };
    if (motionDisabled()) {
      finish();
      return;
    }
    // 减弱档（×0.6）时过渡同比缩短；读不到系数时按完整时长
    const scale = Number(getComputedStyle(document.documentElement).getPropertyValue("--motion-scale"));
    const wait = 200 * (Number.isFinite(scale) && scale > 0 ? scale : 1) + 160;
    const timer = window.setTimeout(finish, wait);
    return () => window.clearTimeout(timer);
  }, [lazy, open, mounted]);

  let body: ReactNode = null;
  if (!lazy) {
    body = typeof children === "function" ? children() : children;
  } else if (open) {
    body = typeof children === "function" ? children() : children;
    frozen.current = body;
  } else if (mounted) {
    body = frozen.current;
  }

  return (
    <div
      ref={ref}
      className={`ui-collapse ${className}`.trim()}
      data-open={open && armed}
      onTransitionEnd={(e) => {
        // 只认本容器自己的高度过渡（嵌套的 Collapse / 行内悬停过渡会冒泡上来）
        if (lazy && !open && e.target === e.currentTarget && e.propertyName === "grid-template-rows") {
          frozen.current = null;
          setMounted(false);
        }
      }}
    >
      <div className="ui-collapse-inner" {...(!open ? ({ inert: "" } as InertProps) : {})}>
        {body}
      </div>
    </div>
  );
}
