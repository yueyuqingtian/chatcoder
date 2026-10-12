/**
 * 浏览器起始页（空白新标签页）
 * plan-128-518：正中一只 2D 嗒嗒（线稿版，不用 3D）+ 一行小提示；网址一律在顶部地址栏输入。
 */
import { Dada } from "../mascot/Dada";

export function BrowserStartPage(_props: { onNavigate?: (url: string) => void }) {
  return (
    <div className="browser-start-page" aria-label="空白标签页">
      <Dada variant="sketch" mood="calm" size={44} className="browser-start-dada" />
      <p className="browser-start-hint">在上方地址栏输入网址</p>
    </div>
  );
}
