import { useState } from "react";
import { Dada } from "../mascot/Dada";
import { IconGlobe, IconSearch } from "../icons";
import { Button, Input } from "../ui";
import { browserSearchTarget, isBrowserHttpUrl, useBrowserLibrary } from "./browserLibrary";

/** plan-771-3177：空白标签直接进入常用网页或搜索。 */
export function BrowserStartPage({ onNavigate }: { onNavigate: (url: string) => void }) {
  const bookmarks = useBrowserLibrary((state) => state.bookmarks);
  const history = useBrowserLibrary((state) => state.history);
  const [query, setQuery] = useState("");
  const visited = new Set<string>();
  const recent = [...history].sort((a, b) => b.visit_count - a.visit_count).filter((item) => {
    if (visited.has(item.url)) return false;
    visited.add(item.url);
    return true;
  }).slice(0, 6);
  const shortcuts = (items: Array<{ url: string; title: string; favicon_url: string | null }>) => (
    <div className="bp-start-shortcuts">
      {items.map((item) => <Button key={item.url} size="sm" variant="secondary" className="bp-start-shortcut"
        icon={item.favicon_url && isBrowserHttpUrl(item.favicon_url) ? <img src={item.favicon_url} alt="" /> : <IconGlobe size={15} />}
        onClick={() => onNavigate(item.url)}><span>{item.title || item.url}</span></Button>)}
    </div>
  );
  return (
    <div className="browser-start-page" aria-label="空白标签页">
      <Dada variant="sketch" mood="calm" size={44} className="browser-start-dada" />
      <form className="bp-start-search" onSubmit={(event) => {
        event.preventDefault();
        if (query.trim()) onNavigate(browserSearchTarget(query));
      }}>
        <Input value={query} onChange={(event) => setQuery(event.target.value)} prefix={<IconSearch size={15} />}
          placeholder="输入网址或搜索关键词" aria-label="搜索或打开网址" />
        <Button type="submit" size="sm" variant="secondary" disabled={!query.trim()}>前往</Button>
      </form>
      {bookmarks.length > 0 && <section className="bp-start-section"><h3>书签</h3>{shortcuts(bookmarks.slice(0, 8))}</section>}
      {recent.length > 0 && <section className="bp-start-section"><h3>常访网站</h3>{shortcuts(recent)}</section>}
      <p className="browser-start-hint">打开网页后，可用工具栏让 AI 分析、诊断或标注页面。</p>
    </div>
  );
}
