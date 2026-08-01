const fs = require('fs');
const path = require('path');
const compDir = 'src/components';
function walk(dir) {
  let out = [];
  for (const f of fs.readdirSync(dir)) {
    const p = path.join(dir, f);
    const st = fs.statSync(p);
    if (st.isDirectory()) out = out.concat(walk(p));
    else if (f.endsWith('.tsx')) out.push(p);
  }
  return out;
}
const classNames = new Set();
for (const f of walk(compDir)) {
  const src = fs.readFileSync(f, 'utf8');
  const re = /className=\{`([^`]+)`\}|className="([^"]+)"/g;
  let m;
  while ((m = re.exec(src))) {
    const cls = m[1] || m[2] || '';
    for (const c of cls.split(/\s+/)) {
      if (c && !c.startsWith('${')) classNames.add(c);
    }
  }
}
const css = fs.readFileSync('src/styles/global.css', 'utf8');
const cssClasses = new Set();
for (const m of css.matchAll(/\.([a-zA-Z][\w-]*)/g)) cssClasses.add(m[1]);
const missing = [...classNames].filter(c => !cssClasses.has(c) && !c.includes(':') && !c.includes('.') && !c.includes('data-'));
console.log('=== 组件中使用但 CSS 中缺失的类名 ===');
console.log(missing.join('\n'));
console.log('=== 共 ' + missing.length + ' 个 ===');
