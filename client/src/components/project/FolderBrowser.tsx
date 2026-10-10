/** 自绘的目录浏览器（plan-20261009-ui U1）——系统选择器弹不出来时的兜底。
 *
 * 点击选择文件夹永远优先走系统自带的选择窗口（桌面版由 Electron 弹出，浏览器环境由本机后端弹出）；
 * 只有后端也弹不出来（后端不在本机 / 无桌面会话 / 缺少系统组件）时，才就地展开本组件，
 * 用后端列出服务器所在机器的目录。用户路径：看到「此电脑」下的盘符与常用位置 → 点进文件夹 → 点「选择此文件夹」。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { Button, Tooltip } from "../ui";
import { IconArrowLeft, IconChevronRight, IconDatabase, IconMonitor } from "../icons";
import { FileIcon } from "../iconkit/FileIcon";

type Listing = Awaited<ReturnType<typeof api.browseDirs>>;

/** 底栏放不下完整路径时保留末尾（最有辨识度的部分），完整路径在悬停提示里 */
function tailPath(p: string, max = 46): string {
  return p.length > max ? `…${p.slice(-(max - 1))}` : p;
}

export function FolderBrowser({ initial, onPick, onCancel }: {
  initial?: string;
  onPick: (path: string) => void;
  onCancel: () => void;
}) {
  const [data, setData] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  /** 只认最后一次请求，快速连点时旧响应不会覆盖新目录 */
  const seq = useRef(0);

  const go = useCallback(async (path: string) => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await api.browseDirs(path);
      if (mine !== seq.current) return;
      setData(res);
      listRef.current?.scrollTo({ top: 0 });
    } catch (e) {
      if (mine !== seq.current) return;
      const raw = e instanceof Error ? e.message.replace(/^Error:\s*/, "") : "";
      // 路由本身不存在（Not Found）= 后端还是旧版本；目录不存在等业务错误自带中文说明
      setError(!raw || raw === "Not Found" ? "后端版本较旧，暂不支持浏览目录，请重启后端后再试" : raw);
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void go(initial ?? ""); }, [go, initial]);

  const atRoot = !data || data.path === "";
  const entries = data?.entries ?? [];

  return (
    <div className="pd-fb" role="group" aria-label="选择文件夹">
      <div className="pd-fb-crumbs" role="navigation" aria-label="当前位置">
        <button type="button" className={"pd-fb-crumb" + (atRoot ? " is-current" : "")} onClick={() => void go("")}>
          <IconMonitor size={13} />
          <span>此电脑</span>
        </button>
        {data?.crumbs.map((c, i) => (
          <span key={c.path} className="pd-fb-crumb-wrap">
            <IconChevronRight size={11} className="pd-fb-sep" />
            <button
              type="button"
              className={"pd-fb-crumb" + (i === data.crumbs.length - 1 ? " is-current" : "")}
              onClick={() => void go(c.path)}
            >
              {c.name}
            </button>
          </span>
        ))}
      </div>

      <div className={"pd-fb-list" + (loading ? " is-loading" : "")} ref={listRef}>
        {atRoot && data && data.places.length > 0 && (
          <div className="pd-fb-places">
            {data.places.map((p) => (
              <button key={p.path} type="button" className="pd-fb-place" onClick={() => void go(p.path)}>
                <FileIcon name={p.name} folder size={15} />
                <span>{p.name}</span>
              </button>
            ))}
          </div>
        )}
        {!atRoot && data && data.parent !== null && (
          <button type="button" className="pd-fb-row is-up" onClick={() => void go(data.parent ?? "")}>
            <IconArrowLeft size={14} />
            <span className="pd-fb-name">返回上一级</span>
          </button>
        )}
        {entries.map((e) => (
          <button key={e.path} type="button" className="pd-fb-row" onClick={() => void go(e.path)} onDoubleClick={() => !atRoot && onPick(e.path)}>
            {atRoot ? <IconDatabase size={15} /> : <FileIcon name={e.name} folder size={16} />}
            <span className="pd-fb-name">{e.name}</span>
            <IconChevronRight size={13} className="pd-fb-go" />
          </button>
        ))}
        {!loading && !error && entries.length === 0 && <div className="pd-fb-empty">这里没有子文件夹，可直接选择当前文件夹</div>}
        {error && <div className="pd-fb-empty is-error" role="alert">{error}</div>}
        {data?.truncated && <div className="pd-fb-empty">文件夹过多，仅显示前 1000 个</div>}
      </div>

      <div className="pd-fb-foot">
        <Tooltip content={data?.path || "先选一个位置"} side="top">
          <span className="pd-fb-cur">{atRoot ? "先选一个位置" : tailPath(data?.path ?? "")}</span>
        </Tooltip>
        <Button size="sm" variant="ghost" onClick={onCancel}>取消</Button>
        <Button size="sm" variant="primary" disabled={atRoot || !data?.path} onClick={() => data?.path && onPick(data.path)}>
          选择此文件夹
        </Button>
      </div>
    </div>
  );
}
