import { useEffect, useRef, useState } from "react";
import { api, type ProviderOut } from "../../api/client";
import { Input, Textarea } from "../ui";

const PRIMARY = "https://newcoder-inner.yinhaiyun.com/ta3api";

function validBase(value: string): boolean {
  try {
    const url = new URL(value);
    return /^(http|https):$/.test(url.protocol) && Boolean(url.hostname)
      && !url.username && !url.password && !url.search && !url.hash && !/\s|\\/.test(value);
  } catch { return false; }
}

/** plan-20261106-ta3-080：草稿保留无效值；串行保存避免慢响应覆盖用户更新的地址。 */
export function Ta3ConnectionFields({ provider, onSaved }: { provider: ProviderOut; onSaved: () => Promise<unknown> }) {
  const [primary, setPrimary] = useState(provider.base_url || PRIMARY);
  const [alternates, setAlternates] = useState((provider.ta3_alt_base_urls || []).join("\n"));
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const queue = useRef<Promise<void>>(Promise.resolve());
  const revision = useRef(0);
  const mounted = useRef(true);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; clearTimeout(timer.current); }; }, []);

  const edit = (base: string, fallback: string) => {
    setPrimary(base); setAlternates(fallback);
    clearTimeout(timer.current);
    const version = ++revision.current;
    const normalized = base.trim().replace(/\/+$/, "");
    const extra = [...new Set(fallback.split(/[,;\r\n]+/).map(v => v.trim().replace(/\/+$/, "")).filter(Boolean))];
    if (!validBase(normalized) || extra.some(v => !validBase(v))) {
      setError("请填写完整 HTTP/HTTPS API 基址，不含账号、查询参数或片段。");
      setStatus(""); return;
    }
    setError(""); setStatus("等待保存…");
    timer.current = setTimeout(() => {
      queue.current = queue.current.then(async () => {
        if (!mounted.current || version !== revision.current) return;
        setStatus("正在保存…");
        try {
          await api.updateProvider(provider.id, { base_url: normalized, ta3_alt_base_urls: extra.filter(v => v !== normalized) });
          if (mounted.current && version === revision.current) {
            setStatus("已保存"); await savedCallback.current();
          }
        } catch (cause) {
          if (mounted.current && version === revision.current) { setError(String(cause)); setStatus("保存失败"); }
        }
      });
    }, 300);
  };

  return <>
    <div className="models-field is-row">
      <label htmlFor={`ta3-primary-${provider.id}`}>主 API 地址</label>
      <Input id={`ta3-primary-${provider.id}`} value={primary} invalid={Boolean(error)}
        placeholder={PRIMARY} onChange={e => edit(e.target.value, alternates)} />
    </div>
    <div className="models-field is-row">
      <label htmlFor={`ta3-alternates-${provider.id}`}>备用 API 地址</label>
      <Textarea id={`ta3-alternates-${provider.id}`} value={alternates} rows={2} invalid={Boolean(error)}
        placeholder="每行一个完整 API 基址；留空则只使用主地址" onChange={e => edit(primary, e.target.value)} />
    </div>
    <div className="models-hint" style={{ padding: 0, lineHeight: 1.6 }}>
      包含服务路径（如 /ta3api）。主地址不可达时依次使用备用地址，修改后自动保存。
      <div>当前连接：{provider.ta3_resolved_base_url || "尚未连接"}</div>
      <div role="status" aria-live="polite" style={{ minHeight: "1.6em", color: error ? "var(--danger)" : undefined }}>{error || status}</div>
    </div>
  </>;
}
