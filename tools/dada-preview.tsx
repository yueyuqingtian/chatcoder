// plan-114-448：嗒嗒三版本预览（丰富版 / 图标版 / 手绘线稿）× 6 配件 × 5 表情
import { renderToStaticMarkup } from "react-dom/server";
import { writeFileSync } from "node:fs";
import { Dada, type DadaMood } from "../client/src/components/mascot/Dada";
import { WORLDS } from "../client/src/store/theme";

const MOODS: DadaMood[] = ["calm", "happy", "think", "oops", "wow"];
const html = renderToStaticMarkup(
  <html>
    <head><meta charSet="utf-8" /><title>嗒嗒三版本预览</title></head>
    <body style={{ fontFamily: "sans-serif", background: "#F7F6F2", padding: 24, color: "#333" }}>
      <h3>丰富版（启动页 / 关于 / 游乐场）· 图标版（应用图标，128/32/16）· 手绘线稿（界面点缀，48/24）</h3>
      <table cellPadding={8}>
        <thead><tr><th />{MOODS.map((m) => <th key={m}>{m}</th>)}<th>图标版</th><th>线稿</th></tr></thead>
        <tbody>
          {WORLDS.map((w) => (
            <tr key={w}>
              <th>{w}</th>
              {MOODS.map((m) => <td key={m}><Dada variant="rich" gear={w} mood={m} size={96} blink={false} /></td>)}
              <td>
                <Dada variant="icon" gear={null} size={64} blink={false} />
                <Dada variant="icon" gear={null} size={32} blink={false} />
                <Dada variant="icon" gear={null} size={16} blink={false} />
              </td>
              <td style={{ color: "#555" }}>
                <Dada variant="sketch" gear={w} size={48} blink={false} />
                <Dada variant="sketch" gear={w} mood="think" size={24} blink={false} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </body>
  </html>,
);
writeFileSync(process.argv[2] ?? "dada-preview.html", "<!DOCTYPE html>" + html);
