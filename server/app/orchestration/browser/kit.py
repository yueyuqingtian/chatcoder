"""页面工具包（page kit）：注入网页主世界的感知与定位脚本（plan-20261015-browser-agent）。

为什么放在服务端、以字符串常量分发：
- 两种宿主（桌面端内置浏览器 / Playwright 兜底）必须用**同一份**元素树、ref、定位与表单逻辑，
  否则同一个 ref 在两条路径上含义不同；
- 桌面端在 `browser.host.welcome` 时一次性拿到源码并缓存，Playwright 宿主直接读取本常量；
- 以 Python 常量（而非 .js 数据文件）存放，打包（PyInstaller）无需额外声明数据文件。

框架（iframe）约定（plan-20261015-082-polish）：
- 同源框架递归展开：元素树、ref、定位、表单与选区都覆盖框架内的文档；
- 所有坐标（rect / x / y）统一为顶层视口坐标，点击仍走真实鼠标事件，由浏览器路由到正确的框架；
- 跨域框架的 DOM 浏览器不允许脚本读取，这里只做标注，提示用截图定位后用坐标操作。

约定：`window.__ccKit.call(fn, args)` 统一入口，返回 `{ok, value}` 或 `{ok:false, error}`，
任何异常都不外抛到宿主（宿主只需区分“脚本执行失败”与“动作语义失败”）。
ref 以 WeakMap 绑定元素：同一文档内同一元素的 ref 始终不变，导航后整体失效。
"""
from __future__ import annotations

# 每次修改 KIT_SOURCE 都必须递增：已打开的页面靠版本号判断是否需要重新注入新的页面工具包
KIT_VERSION = 5

KIT_SOURCE = r"""
(() => {
  const V = %VERSION%;
  if (window.__ccKit && window.__ccKit.v === V) return 'present';
  const refs = new Map();
  const ids = new WeakMap();
  let seq = 0;
  const MAX_NAME = 80;
  const MAX_FRAMES = 4;

  const INTERACTIVE_ROLES = new Set(['button','link','checkbox','radio','switch','tab','menuitem','menuitemcheckbox',
    'menuitemradio','option','textbox','searchbox','combobox','slider','spinbutton','treeitem','listbox']);
  const LANDMARKS = { header: 'banner', nav: 'navigation', main: 'main', aside: 'complementary', footer: 'contentinfo',
    form: 'form', dialog: 'dialog', section: null, article: 'article' };
  // 结构角色：交互视图只保留能帮助定位的分区；完整视图再加列表/表格等内容分组
  const STRUCT_ROLES = new Set(['banner','navigation','main','complementary','contentinfo','form','dialog',
    'alertdialog','search','tablist','menu','menubar','toolbar','tree','grid']);
  const STRUCT_ALL = new Set(['article','region','table','list']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','BASE','TITLE']);

  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const q = (s) => JSON.stringify(s);

  // ── 框架感知 ───────────────────────────────────────────
  // 跨文档的元素不能用 instanceof Element 判断（每个框架有自己的 realm），统一用 nodeType
  const isEl = (x) => !!x && x.nodeType === 1;
  const winOf = (node) => (node && node.ownerDocument && node.ownerDocument.defaultView) || window;
  const isFrame = (el) => isEl(el) && (el.tagName === 'IFRAME' || el.tagName === 'FRAME');
  /** 框架元素对应的同源文档；跨域或尚未加载时返回 null（浏览器不允许脚本读取） */
  function subDoc(fe) {
    try { const d = fe.contentDocument; return d && d.documentElement ? d : null; } catch (e) { return null; }
  }
  /** 文档所在的框架元素（顶层文档返回 null） */
  function frameEl(doc) {
    try { return doc.defaultView ? doc.defaultView.frameElement : null; } catch (e) { return null; }
  }
  function frameName(fe) {
    const t = clip(fe.getAttribute('title') || fe.getAttribute('aria-label') || fe.getAttribute('name') || '', 40);
    if (t) return t;
    const src = fe.getAttribute('src');
    if (!src) return '';
    try { return new URL(src, location.href).host || ''; } catch (e) { return ''; }
  }
  /** 顶层文档与所有同源框架文档（深度与数量有上限），按文档顺序返回 */
  function docsOf() {
    const out = [document];
    const walkDocs = (doc, depth) => {
      if (depth >= MAX_FRAMES || out.length >= 40) return;
      for (const fe of doc.querySelectorAll('iframe,frame')) {
        const d = subDoc(fe);
        if (d && !out.includes(d)) { out.push(d); walkDocs(d, depth + 1); }
      }
    };
    walkDocs(document, 0);
    return out;
  }
  /** 跨域框架（内容不可读）的可见元素 */
  function crossFrameList() {
    const out = [];
    for (const d of docsOf()) {
      for (const fe of d.querySelectorAll('iframe,frame')) if (!subDoc(fe) && visible(fe)) out.push(fe);
    }
    return out;
  }
  /** 框架内坐标 → 顶层视口坐标的偏移：逐层累加 iframe 的位置与边框 */
  function frameOffset(doc) {
    let x = 0, y = 0, d = doc;
    for (let i = 0; d && d !== document && i <= MAX_FRAMES; i++) {
      const fe = frameEl(d);
      if (!fe) break;
      const r = fe.getBoundingClientRect();
      x += r.left + fe.clientLeft; y += r.top + fe.clientTop;
      d = fe.ownerDocument;
    }
    return { x, y };
  }
  /** 元素在顶层视口中的矩形 */
  function absRect(el) {
    const r = el.getBoundingClientRect();
    const o = frameOffset(el.ownerDocument);
    return { left: r.left + o.x, top: r.top + o.y, right: r.right + o.x, bottom: r.bottom + o.y, width: r.width, height: r.height };
  }
  /** 当前获得焦点的元素（穿过同源框架）；落在 body 上时视为没有焦点 */
  function deepActive() {
    let el = document.activeElement;
    for (let i = 0; el && isFrame(el) && i < MAX_FRAMES; i++) {
      const sd = subDoc(el);
      if (!sd || !sd.activeElement) break;
      el = sd.activeElement;
    }
    return el && el !== document.body && el !== document.documentElement ? el : null;
  }
  /** 在所有同源文档中按选择器查找第一个匹配 */
  function queryAny(sel) {
    let found = null;
    try {
      for (const d of docsOf()) { found = d.querySelector(sel); if (found) break; }
    } catch (e) { throw new Error('选择器无效：' + sel); }
    return found;
  }
  function textHas(text) {
    const want = String(text).toLowerCase();
    return docsOf().some((d) => ((d.body && d.body.innerText) || '').toLowerCase().includes(want));
  }

  function refOf(el) {
    let id = ids.get(el);
    if (!id) { id = 'ref_' + (++seq); ids.set(el, id); refs.set(id, new WeakRef(el)); }
    return id;
  }
  function byRef(ref) {
    const r = refs.get(String(ref).trim());
    const el = r && r.deref();
    return el && el.isConnected ? el : null;
  }

  function visible(el) {
    if (!isEl(el)) return false;
    if (typeof el.checkVisibility === 'function') {
      if (!el.checkVisibility({ checkVisibilityCSS: true })) return false;
    } else {
      const s = winOf(el).getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.getClientRects().length > 0;
  }

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = el.tagName;
    switch (tag) {
      case 'A': return el.hasAttribute('href') ? 'link' : '';
      case 'BUTTON': return 'button';
      case 'SUMMARY': return 'button';
      case 'SELECT': return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'OPTION': return 'option';
      case 'IMG': return el.getAttribute('alt') ? 'img' : '';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'DIALOG': return 'dialog';
      case 'TABLE': return 'table';
      case 'UL': case 'OL': return 'list';
      case 'INPUT': {
        const t = (el.type || 'text').toLowerCase();
        if (t === 'hidden') return '';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'range') return 'slider';
        if (t === 'number') return 'spinbutton';
        if (t === 'search') return 'searchbox';
        if (['button','submit','reset','image'].includes(t)) return 'button';
        return 'textbox';
      }
    }
    if (LANDMARKS[tag.toLowerCase()]) return LANDMARKS[tag.toLowerCase()];
    if (el.isContentEditable && (!el.parentElement || !el.parentElement.isContentEditable)) return 'textbox';
    return '';
  }

  function isInteractive(el, role) {
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (el.tagName === 'INPUT' && el.type !== 'hidden') return true;
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    if (ti != null && Number(ti) >= 0) {
      if (el.tagName !== 'DIV' && el.tagName !== 'SECTION') return true;
      if ((el.textContent || '').trim().length > 0 && el.childElementCount <= 3) return true;
    }
    if (el.tagName === 'LABEL' && el.control && !visible(el.control)) return true;
    // 无语义的可点击块（div/span + cursor:pointer）：cursor 会继承，只认「指针从这里开始」的那一层，
    // 否则可点击卡片里的每个子元素都会被重复列出
    if (el.childElementCount <= 4 && (el.textContent || '').length <= 200 && el.parentElement) {
      try {
        const cs = winOf(el).getComputedStyle(el);
        if (cs.cursor === 'pointer' && winOf(el).getComputedStyle(el.parentElement).cursor !== 'pointer'
            && !el.closest('a,button,label,[role=button],[role=link],[role=menuitem],[role=tab],[role=option]')) {
          const t = (el.innerText || '').trim();
          return t.length > 0 && t.length <= 60;
        }
      } catch (e) {}
    }
    return false;
  }

  /** display:contents 的包裹层没有盒子（checkVisibility 为 false），但子元素照常可见 */
  function isContents(el) { try { return winOf(el).getComputedStyle(el).display === 'contents'; } catch (e) { return false; } }

  function textOf(el) { return clip(el.innerText != null ? el.innerText : el.textContent, MAX_NAME); }

  function nameOf(el) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((id) => { const n = el.ownerDocument.getElementById(id); return n ? n.textContent : ''; }).join(' ');
      if (clip(t, MAX_NAME)) return clip(t, MAX_NAME);
    }
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return clip(al, MAX_NAME);
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      const t = (el.type || '').toLowerCase();
      if (['button','submit','reset'].includes(t)) return clip(el.value || (t === 'submit' ? 'Submit' : ''), MAX_NAME);
      if (t === 'image') return clip(el.alt || el.value || 'image', MAX_NAME);
      if (el.labels && el.labels.length) {
        const t2 = Array.from(el.labels).map((l) => l.innerText || l.textContent || '').join(' ');
        if (clip(t2, MAX_NAME)) return clip(t2, MAX_NAME);
      }
      if (el.placeholder) return clip(el.placeholder, MAX_NAME);
      if (el.title) return clip(el.title, MAX_NAME);
      if (el.name) return clip(el.name, MAX_NAME);
      return '';
    }
    if (tag === 'IMG') return clip(el.alt || el.title || '', MAX_NAME);
    const txt = textOf(el);
    if (txt) return txt;
    if (el.title) return clip(el.title, MAX_NAME);
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img) return clip(img.alt, MAX_NAME);
    const svgTitle = el.querySelector && el.querySelector('svg title');
    if (svgTitle) return clip(svgTitle.textContent, MAX_NAME);
    return '';
  }

  /** 分区 / 容器只用显式标注命名：拿整段正文当名字只会得到又长又无用的标签 */
  function structName(el) {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return clip(al, MAX_NAME);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map((id) => { const n = el.ownerDocument.getElementById(id); return n ? n.textContent : ''; }).join(' ');
      if (clip(t, MAX_NAME)) return clip(t, MAX_NAME);
    }
    return clip(el.getAttribute('title') || '', MAX_NAME);
  }

  const ROLE_ZH = { button: '按钮', link: '链接', textbox: '输入框', searchbox: '搜索框', combobox: '下拉框', checkbox: '复选框',
    radio: '单选框', switch: '开关', tab: '标签', menuitem: '菜单项', option: '选项', slider: '滑块', spinbutton: '数字框',
    heading: '标题', img: '图片', listbox: '列表框', treeitem: '树节点', dialog: '对话框', main: '主体区域',
    navigation: '导航', banner: '页头', contentinfo: '页脚', form: '表单', list: '列表', table: '表格' };
  function labelOf(el) {
    if (!isEl(el)) return '页面';
    if (isFrame(el)) { const nm = frameName(el); return '框架' + (nm ? '「' + nm + '」' : ''); }
    if (el === document.documentElement || el === document.body) return '页面';
    // 框架文档的根节点：命中的其实是框架本身（例如透明的遮罩框架），按框架标注
    const fe = el.ownerDocument && el.ownerDocument !== document ? frameEl(el.ownerDocument) : null;
    if (fe && (el === el.ownerDocument.documentElement || el === el.ownerDocument.body)) return labelOf(fe);
    const role = roleOf(el) || el.tagName.toLowerCase();
    const container = STRUCT_ROLES.has(role) || STRUCT_ALL.has(role) || (!INTERACTIVE_ROLES.has(role) && el.childElementCount > 3);
    const name = container ? structName(el) : nameOf(el);
    const zh = ROLE_ZH[role] || role;
    return name ? zh + '「' + clip(name, 30) + '」' : zh;
  }

  function states(el, role) {
    const out = [];
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') out.push(el.checked ? 'checked' : 'unchecked');
      else if (!['button','submit','reset','image'].includes(t)) {
        if (t === 'password') out.push('value=' + (el.value ? '"••••"' : '""'));
        else out.push('value=' + q(clip(el.value, 60)));
        if (t !== 'text' && tag === 'INPUT') out.push('type=' + t);
        if (el.placeholder && nameOf(el) !== clip(el.placeholder, MAX_NAME)) out.push('placeholder=' + q(clip(el.placeholder, 40)));
      }
      if (el.required) out.push('required');
      if (el.readOnly) out.push('readonly');
    } else if (tag === 'SELECT') {
      const o = el.selectedOptions && el.selectedOptions[0];
      out.push('value=' + q(o ? clip(o.textContent, 40) : ''));
      out.push('options=' + el.options.length);
    } else if (el.isContentEditable && role === 'textbox') {
      out.push('value=' + q(clip(el.innerText, 60)));
    }
    const ac = el.getAttribute('aria-checked'); if (ac && tag !== 'INPUT') out.push(ac === 'true' ? 'checked' : (ac === 'mixed' ? 'mixed' : 'unchecked'));
    const ae = el.getAttribute('aria-expanded'); if (ae) out.push(ae === 'true' ? 'expanded' : 'collapsed');
    const as = el.getAttribute('aria-selected'); if (as === 'true') out.push('selected');
    const ap = el.getAttribute('aria-pressed'); if (ap === 'true') out.push('pressed');
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') out.push('disabled');
    if (role === 'link') {
      const href = el.getAttribute('href') || '';
      if (href && !href.startsWith('javascript:')) {
        let h = href;
        try { const u = new URL(href, el.ownerDocument.location.href); h = u.origin === location.origin ? (u.pathname + u.search + u.hash) : u.href; } catch (e) {}
        out.push('href=' + q(clip(h, 80)));
      }
    }
    if (role === 'heading') { const lv = /^H(\d)$/.exec(tag); out.push('level=' + (lv ? lv[1] : (el.getAttribute('aria-level') || '2'))); }
    if (el.ownerDocument.activeElement === el) out.push('focused');
    return out;
  }

  function kids(node) {
    const list = [];
    if (node.shadowRoot && node.shadowRoot.mode === 'open') list.push(...node.shadowRoot.children);
    if (node.tagName === 'SLOT') { list.push(...node.assignedElements({ flatten: true })); return list; }
    list.push(...node.children);
    return list;
  }

  function ownText(el) {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\s+/g, ' ').trim();
  }

  function inViewport(el) {
    const r = absRect(el);
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  }

  function snapshot(a) {
    a = a || {};
    const filter = a.filter === 'all' ? 'all' : 'interactive';
    const maxChars = Math.max(1000, Math.min(Number(a.max_chars) || 12000, 60000));
    const query = a.query ? String(a.query).toLowerCase() : '';
    const viewportOnly = !!a.viewport_only;
    let root = document.body || document.documentElement;
    if (a.ref) { const el = byRef(a.ref); if (!el) throw new Error(a.ref + ' 已失效：页面已变化或已导航，请重新执行 browser_snapshot'); root = el; }
    const lines = [];
    let chars = 0, truncated = false, interactive = 0, offscreen = 0, hiddenByQuery = 0, crossFrames = 0, frameLevel = 0;
    const emit = (depth, text) => {
      if (truncated) return;
      const line = '  '.repeat(depth) + '- ' + text;
      if (chars + line.length + 1 > maxChars) { truncated = true; return; }
      lines.push(line); chars += line.length + 1;
    };
    const walk = (node, depth) => {
      for (const el of kids(node)) {
        if (truncated) return;
        if (SKIP.has(el.tagName)) continue;
        if (isFrame(el)) {
          if (!visible(el)) continue;
          const sd = subDoc(el);
          if (!sd) {
            crossFrames++;
            emit(depth, labelOf(el) + '（跨域：内容无法直接读取，请截图查看，再用坐标点击或输入）');
            continue;
          }
          emit(depth, labelOf(el));
          if (frameLevel < MAX_FRAMES && sd.body) {
            frameLevel++;
            walk(sd.body, depth + 1);
            frameLevel--;
          }
          continue;
        }
        if (!visible(el)) { if (isContents(el)) walk(el, depth); continue; }
        const role = roleOf(el);
        const inter = isInteractive(el, role);
        if (inter) {
          interactive++;
          const onScreen = inViewport(el);
          if (!onScreen) offscreen++;
          if (viewportOnly && !onScreen) { if (role === 'listbox' || role === 'combobox') continue; walk(el, depth); continue; }
          const name = nameOf(el);
          const st = states(el, role);
          const text = (role || el.tagName.toLowerCase()) + (name ? ' ' + q(name) : '') + ' [' + refOf(el) + ']' + (st.length ? ' ' + st.join(' ') : '');
          if (!query || text.toLowerCase().includes(query)) emit(depth, text); else hiddenByQuery++;
          if (el.tagName === 'SELECT' || role === 'textbox' || role === 'searchbox' || role === 'button' || role === 'link' || role === 'option' || role === 'checkbox' || role === 'radio' || role === 'switch' || role === 'slider' || role === 'spinbutton') continue;
          walk(el, depth + 1);
          continue;
        }
        if (role === 'heading') {
          const text = 'heading ' + q(nameOf(el)) + ' [' + refOf(el) + '] ' + states(el, role).join(' ');
          if (!query || text.toLowerCase().includes(query)) emit(depth, text.trim());
          continue;
        }
        if ((STRUCT_ROLES.has(role) || (filter === 'all' && STRUCT_ALL.has(role))) && !query) {
          const nm = structName(el);
          emit(depth, role + (nm ? ' ' + q(clip(nm, 40)) : ''));
          walk(el, depth + 1);
          continue;
        }
        if (filter === 'all' && !query) {
          const t = ownText(el);
          if (t.length >= 2) {
            const tag = el.tagName;
            const kind = tag === 'P' ? 'paragraph' : (tag === 'LI' ? 'listitem' : (tag === 'TD' || tag === 'TH' ? 'cell' : (tag === 'LABEL' ? 'label' : 'text')));
            emit(depth, kind + ' ' + q(clip(t, 160)));
          } else if (el.tagName === 'IMG' && el.alt) {
            emit(depth, 'img ' + q(clip(el.alt, 60)) + ' [' + refOf(el) + ']');
          }
        }
        walk(el, depth);
      }
    };
    walk(root, 0);
    const dlg = openDialog();
    const head = [];
    head.push('# 页面: ' + clip(document.title || '(无标题)', 80) + ' — ' + location.href);
    head.push('# 视口 ' + innerWidth + '×' + innerHeight + '，滚动 ' + Math.round(scrollY) + '/' + Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)) + '；可交互元素 ' + interactive + ' 个（视口外 ' + offscreen + ' 个，点击时会自动滚入）');
    if (dlg) head.push('# 注意：页面有打开的对话框 ' + labelOf(dlg) + '，后面的元素可能被遮挡');
    if (query) head.push('# 关键词「' + a.query + '」匹配 ' + lines.length + ' 项');
    const tail = [];
    if (truncated) tail.push('（内容已截断：用 ref 指定子树、query 过滤或调大 max_chars 查看其余部分）');
    if (crossFrames) tail.push('（页面含 ' + crossFrames + ' 个跨域框架，其内部内容无法读取：请截图查看，再用坐标点击或输入）');
    if (!lines.length) tail.push(query ? '（没有匹配的元素）' : '（未找到可见的可交互元素）');
    return { text: head.concat(lines, tail).join('\n'), count: lines.length, truncated, url: location.href, title: document.title, cross_frames: crossFrames };
  }

  function openDialog() {
    for (const d of docsOf()) {
      const cands = d.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]');
      for (const c of cands) if (visible(c)) return c;
    }
    return null;
  }

  /** 顶层视口坐标处最上层的元素：逐层进入同源框架与 open shadow root */
  function deepFromPoint(x, y) {
    let doc = document, px = x, py = y, el = null;
    for (let i = 0; i <= MAX_FRAMES; i++) {
      el = doc.elementFromPoint(px, py);
      while (el && el.shadowRoot && el.shadowRoot.elementFromPoint) {
        const inner = el.shadowRoot.elementFromPoint(px, py);
        if (!inner || inner === el) break;
        el = inner;
      }
      if (!isFrame(el)) return el;
      const sd = subDoc(el);
      if (!sd) return el;   // 跨域框架：命中的就是框架本身
      const fr = el.getBoundingClientRect();
      px -= fr.left + el.clientLeft; py -= fr.top + el.clientTop;
      doc = sd;
    }
    return el;
  }

  /** a 是否包含 b（沿 DOM / shadow 宿主 / 框架边界向上查找） */
  function composedContains(a, b) {
    while (b) {
      if (a === b) return true;
      b = b.parentNode || b.host || (b.nodeType === 9 ? frameEl(b) : null) || null;
    }
    return false;
  }

  function findByText(text) {
    const want = String(text).replace(/\s+/g, ' ').trim().toLowerCase();
    if (!want) return null;
    let best = null, bestScore = 0, scanned = 0;
    outer: for (const d of docsOf()) {
      const walker = d.createTreeWalker(d.body || d.documentElement, NodeFilter.SHOW_ELEMENT);
      for (let el = walker.currentNode; el && scanned < 8000; el = walker.nextNode()) {
        scanned++;
        if (SKIP.has(el.tagName)) continue;
        const role = roleOf(el);
        const inter = isInteractive(el, role);
        let name = '';
        if (inter) name = nameOf(el);
        else if (el.childElementCount <= 2) name = textOf(el);
        if (!name) continue;
        const n = name.toLowerCase();
        let score = n === want ? 6 : (n.startsWith(want) ? 4 : (n.includes(want) ? 2 : 0));
        if (!score) continue;
        if (inter) score += 3;
        if (!visible(el)) continue;
        if (inViewport(el)) score += 1;
        if (score > bestScore) { best = el; bestScore = score; if (score >= 10) break outer; }
      }
    }
    return best;
  }

  function pick(a) {
    if (a.ref) { const el = byRef(a.ref); if (!el) throw new Error(a.ref + ' 已失效：页面已变化或已导航，请重新执行 browser_snapshot'); return el; }
    if (a.selector) {
      const el = queryAny(a.selector);
      if (!el) throw new Error('未找到匹配选择器 ' + a.selector + ' 的元素');
      return el;
    }
    if (a.text) { const el = findByText(a.text); if (!el) throw new Error('未找到文字为「' + a.text + '」的可见元素'); return el; }
    return null;
  }

  /** 元素在顶层视口中的矩形（取第一个落在视口内的盒子；坐标已换算到顶层） */
  function rectOf(el) {
    const o = frameOffset(el.ownerDocument);
    const rs = Array.from(el.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
    const r = rs.find((x) => x.bottom + o.y > 0 && x.right + o.x > 0 && x.top + o.y < innerHeight && x.left + o.x < innerWidth)
      || rs[0] || el.getBoundingClientRect();
    return { x: Math.round(r.left + o.x), y: Math.round(r.top + o.y), width: Math.round(r.width), height: Math.round(r.height) };
  }

  function resolve(a) {
    a = a || {};
    if (!a.ref && !a.selector && !a.text && a.x != null && a.y != null) {
      const x = Number(a.x), y = Number(a.y);
      const el = deepFromPoint(x, y);
      return { x, y, label: el ? labelOf(el) : '坐标 (' + x + ', ' + y + ')', rect: el ? rectOf(el) : null, ref: el ? refOf(el) : null,
        editable: !!(el && isEditable(el)), point: true };
    }
    const el = pick(a);
    if (!el) throw new Error('需要提供 ref、selector、text 或 x/y 之一');
    if (!el.isConnected) throw new Error('目标元素已从页面移除，请重新执行 browser_snapshot');
    if (!visible(el)) {
      const lbl = el.labels && el.labels[0];
      if (lbl && visible(lbl)) return resolve(Object.assign({}, a, { ref: refOf(lbl), selector: null, text: null }));
      throw new Error(labelOf(el) + ' 当前不可见（被隐藏或尺寸为 0）');
    }
    const ar = absRect(el);
    const fully = ar.top >= 0 && ar.left >= 0 && ar.bottom <= innerHeight && ar.right <= innerWidth;
    if (a.scroll !== false && !fully) { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); }
    const rect = rectOf(el);
    const pts = [[0.5, 0.5], [0.25, 0.5], [0.75, 0.5], [0.5, 0.25], [0.5, 0.75]];
    let pt = null, cover = null;
    for (const [fx, fy] of pts) {
      const x = Math.round(rect.x + rect.width * fx), y = Math.round(rect.y + rect.height * fy);
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const hit = deepFromPoint(x, y);
      if (hit && (composedContains(el, hit) || composedContains(hit, el) || (el.labels && Array.from(el.labels).some((l) => composedContains(l, hit))))) { pt = { x, y }; break; }
      if (!cover && hit) cover = hit;
    }
    const out = { rect, label: labelOf(el), ref: refOf(el), role: roleOf(el), editable: isEditable(el),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true') };
    if (pt) { out.x = pt.x; out.y = pt.y; }
    else {
      out.x = Math.round(rect.x + rect.width / 2); out.y = Math.round(rect.y + rect.height / 2);
      if (cover) out.covered_by = labelOf(cover);
      if (out.x < 0 || out.y < 0 || out.x >= innerWidth || out.y >= innerHeight) out.offscreen = true;
    }
    return out;
  }

  function isEditable(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === 'TEXTAREA') return !el.readOnly && !el.disabled;
    if (el.tagName === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      return !['checkbox','radio','button','submit','reset','image','file','range','color','hidden'].includes(t) && !el.readOnly && !el.disabled;
    }
    return false;
  }

  function focusTarget(a) {
    a = a || {};
    let el = pick(a);
    if (!el) {
      el = deepActive();
      if (!el) throw new Error('当前页面没有获得焦点的输入框，请用 ref / selector / text 指定目标');
    }
    if (!isEditable(el)) {
      const inner = el.querySelector && el.querySelector('input:not([type=hidden]),textarea,[contenteditable=""],[contenteditable=true]');
      if (inner && isEditable(inner)) el = inner;
    }
    if (!isEditable(el)) throw new Error(labelOf(el) + ' 不是可输入的元素');
    if (a.scroll !== false) {
      const r = absRect(el);
      if (r.top < 0 || r.bottom > innerHeight) el.scrollIntoView({ block: 'center', behavior: 'instant' });
    }
    el.focus({ preventScroll: true });
    if (a.clear) selectAllIn(el);
    else if (el.isContentEditable) {
      const d = el.ownerDocument; const sel = d.getSelection(); const range = d.createRange();
      range.selectNodeContents(el); range.collapse(false); sel.removeAllRanges(); sel.addRange(range);
    } else {
      try { const n = el.value.length; el.setSelectionRange(n, n); } catch (e) {}
    }
    return { label: labelOf(el), ref: refOf(el), rect: rectOf(el), value: el.isContentEditable ? clip(el.innerText, 200) : clip(el.value, 200) };
  }

  function selectAllIn(el) {
    if (el.isContentEditable) {
      const d = el.ownerDocument; const sel = d.getSelection(); const range = d.createRange();
      range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
    } else if (typeof el.select === 'function') {
      el.select();
    }
  }

  function selectAll(a) {
    a = a || {};
    const el = pick(a);
    if (el) {
      if (isEditable(el)) { el.focus({ preventScroll: true }); selectAllIn(el); }
      else {
        const d = el.ownerDocument; const sel = d.getSelection(); const range = d.createRange();
        range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
      }
      return { label: labelOf(el), text: clip(String(el.ownerDocument.getSelection()), 4000) };
    }
    const act = deepActive();
    if (act && isEditable(act)) { selectAllIn(act); return { label: labelOf(act), text: clip(act.isContentEditable ? String(act.ownerDocument.getSelection()) : act.value, 4000) }; }
    const dd = act ? act.ownerDocument : document;
    dd.execCommand('selectAll');
    return { label: '整个页面', text: clip(String(dd.getSelection()), 4000) };
  }

  function selection() {
    const act = deepActive();
    if (act && (act.tagName === 'INPUT' || act.tagName === 'TEXTAREA') && typeof act.selectionStart === 'number') {
      const s = act.value.slice(act.selectionStart, act.selectionEnd);
      if (s) return { text: s, label: labelOf(act) };
    }
    const d = act ? act.ownerDocument : document;
    return { text: String(d.getSelection() || ''), label: act ? labelOf(act) : '页面' };
  }

  function cutSelection() {
    const act = deepActive();
    const sel = selection();
    if (!sel.text) return sel;
    if (act && isEditable(act)) {
      if (!act.ownerDocument.execCommand('delete')) {
        if (act.tagName === 'INPUT' || act.tagName === 'TEXTAREA') {
          const s = act.selectionStart, e = act.selectionEnd;
          act.setRangeText('', s, e, 'start');
          act.dispatchEvent(new (winOf(act).Event)('input', { bubbles: true }));
        }
      }
    }
    return sel;
  }

  function paste(a) {
    const act = deepActive() || document.body;
    let handled = false;
    try {
      const w = winOf(act);
      const dt = new w.DataTransfer();
      dt.setData('text/plain', String(a.text || ''));
      const ev = new w.ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true, composed: true });
      handled = !act.dispatchEvent(ev);
    } catch (e) {}
    return { handled, label: labelOf(act), editable: isEditable(act) };
  }

  function setNative(el, value) {
    const w = winOf(el);
    const proto = el.tagName === 'TEXTAREA' ? w.HTMLTextAreaElement.prototype : (el.tagName === 'SELECT' ? w.HTMLSelectElement.prototype : w.HTMLInputElement.prototype);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
  }

  function fill(a) {
    a = a || {};
    const el = pick(a);
    if (!el) throw new Error('需要提供 ref、selector 或 text');
    const v = a.value;
    const tag = el.tagName;
    const w = winOf(el);
    const fire = () => { el.dispatchEvent(new w.Event('input', { bubbles: true })); el.dispatchEvent(new w.Event('change', { bubbles: true })); };
    if (tag === 'SELECT') {
      const wants = (Array.isArray(v) ? v : [v]).map((x) => String(x).trim().toLowerCase());
      let hit = 0;
      for (const o of el.options) {
        const m = wants.includes(String(o.value).toLowerCase()) || wants.includes(clip(o.textContent, 200).toLowerCase());
        if (el.multiple) o.selected = m; else if (m && !hit) { setNative(el, o.value); }
        if (m) hit++;
      }
      if (!hit) throw new Error(labelOf(el) + ' 没有值为「' + wants.join('、') + '」的选项，可选：' + Array.from(el.options).slice(0, 15).map((o) => clip(o.textContent, 20)).join('、'));
      fire();
      return { label: labelOf(el), value: Array.from(el.selectedOptions).map((o) => clip(o.textContent, 40)).join('、') };
    }
    const type = (el.type || '').toLowerCase();
    const role = roleOf(el);
    if (type === 'checkbox' || type === 'radio' || role === 'checkbox' || role === 'switch' || role === 'radio') {
      const want = typeof v === 'boolean' ? v : !['false', '0', 'off', 'no', ''].includes(String(v).trim().toLowerCase());
      const cur = tag === 'INPUT' ? el.checked : el.getAttribute('aria-checked') === 'true';
      if (cur !== want) el.click();
      const now = tag === 'INPUT' ? el.checked : el.getAttribute('aria-checked') === 'true';
      return { label: labelOf(el), value: now ? 'checked' : 'unchecked' };
    }
    if (el.isContentEditable) {
      el.focus({ preventScroll: true }); selectAllIn(el);
      if (!el.ownerDocument.execCommand('insertText', false, String(v == null ? '' : v))) el.innerText = String(v == null ? '' : v);
      return { label: labelOf(el), value: clip(el.innerText, 80) };
    }
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (el.disabled || el.readOnly) throw new Error(labelOf(el) + ' 处于禁用或只读状态');
      el.focus({ preventScroll: true });
      setNative(el, String(v == null ? '' : v));
      fire();
      return { label: labelOf(el), value: type === 'password' ? '••••' : clip(el.value, 80) };
    }
    throw new Error(labelOf(el) + ' 不是表单控件');
  }

  function pageText(a) {
    a = a || {};
    const max = Math.max(500, Math.min(Number(a.max_chars) || 8000, 100000));
    let el = null;
    if (a.ref || a.selector || a.text) el = pick(a);
    if (!el) {
      const cands = [];
      for (const d of docsOf()) for (const c of d.querySelectorAll('main,[role=main],article')) if (visible(c)) cands.push(c);
      el = cands.sort((x, y) => (y.innerText || '').length - (x.innerText || '').length)[0] || document.body;
    }
    let t = (el.innerText || el.textContent || '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    const total = t.length;
    if (t.length > max) t = t.slice(0, max) + '\n…（已截断，共 ' + total + ' 字）';
    return { text: t, total, label: el === document.body ? '整个页面' : labelOf(el), url: location.href, title: document.title,
      cross_frames: crossFrameList().length };
  }

  // 变化监听覆盖顶层与各同源框架文档；框架新加载后，下一次 watch 时补上
  const observed = new WeakSet();
  let mo = null, mut = 0, last = 0;
  function observeAll() {
    if (!mo) mo = new MutationObserver((recs) => { mut += recs.length; last = performance.now(); });
    for (const d of docsOf()) {
      if (observed.has(d)) continue;
      observed.add(d);
      try { mo.observe(d, { subtree: true, childList: true, attributes: true, characterData: true }); } catch (e) {}
    }
  }
  function watch() {
    observeAll();
    mut = 0; last = performance.now();
    return true;
  }
  function state() {
    const act = deepActive();
    const dlg = openDialog();
    return { url: location.href, title: document.title, ready: document.readyState,
      focus: act ? labelOf(act) : '',
      dialog: dlg ? labelOf(dlg) : '', mutations: mut,
      // 距最近一次 DOM 变更（或 watch 起点）多少毫秒：宿主据此在自己一侧轮询「稳定」，不依赖页面计时器
      idle_ms: mo ? Math.round(performance.now() - last) : 1e9,
      scroll: { x: Math.round(scrollX), y: Math.round(scrollY), max_y: Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)) },
      viewport: { width: innerWidth, height: innerHeight } };
  }
  function quiet(a) {
    a = a || {};
    const quietMs = Math.max(30, Number(a.quiet_ms) || 120);
    const timeout = Math.max(quietMs, Number(a.timeout_ms) || 1500);
    const start = performance.now();
    if (!mo) watch();
    return new Promise((resolve) => {
      const tick = () => {
        const now = performance.now();
        if (now - last >= quietMs || now - start >= timeout) { const s = state(); s.waited = Math.round(now - start); resolve(s); }
        else setTimeout(tick, 25);
      };
      setTimeout(tick, Math.min(quietMs, 50));
    });
  }

  function scrollTo(a) {
    a = a || {};
    if (a.to === 'top') window.scrollTo({ top: 0, behavior: 'instant' });
    else if (a.to === 'bottom') window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' });
    else {
      const el = pick(a);
      if (!el) throw new Error('需要提供 ref / selector / text 或 to=top|bottom');
      el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
      return Object.assign(state(), { label: labelOf(el), rect: rectOf(el) });
    }
    return state();
  }

  /** 视口坐标处可滚动的容器（可跨进入同源框架，框架内的滚动容器同样适用） */
  function scrollableAt(x, y) {
    let el = deepFromPoint(x, y);
    while (el && el.ownerDocument && el !== el.ownerDocument.body && el !== el.ownerDocument.documentElement) {
      const s = winOf(el).getComputedStyle(el);
      if ((/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 2) || (/(auto|scroll)/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 2)) return el;
      el = el.parentElement;
    }
    return null;
  }
  function scrollPos(a) {
    const hasXY = a && a.x != null && a.y != null;
    const x = hasXY ? Number(a.x) : 0, y = hasXY ? Number(a.y) : 0;
    const el = hasXY ? scrollableAt(x, y) : null;
    if (el) return { inner: true, label: labelOf(el), top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight), left: Math.round(el.scrollLeft) };
    // 落点在同源框架内、且框架文档本身可滚动：读取该框架的滚动位置
    if (hasXY) {
      const under = deepFromPoint(x, y);
      const fd = under && under.ownerDocument;
      if (fd && fd !== document && fd.defaultView) {
        const w = fd.defaultView; const se = fd.scrollingElement || fd.documentElement;
        const fe = frameEl(fd);
        return { inner: true, label: '框架' + (fe && frameName(fe) ? '「' + frameName(fe) + '」' : ''), top: Math.round(w.scrollY),
          max: Math.max(0, Math.round(se.scrollHeight - w.innerHeight)), left: Math.round(w.scrollX) };
      }
    }
    return { inner: false, label: '页面', top: Math.round(scrollY), max: Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)), left: Math.round(scrollX) };
  }

  /** 条件检查（同步）：宿主侧轮询用；Playwright 宿主仍可用下面的 waitFor 在页面内等待 */
  function check(a) {
    a = a || {};
    let ok;
    if (a.text) {
      const has = textHas(a.text);
      ok = a.gone ? !has : has;
    } else if (a.selector) {
      const el = queryAny(a.selector);
      const vis = !!(el && visible(el));
      ok = a.gone ? !vis : vis;
    } else if (a.ref) {
      const el = byRef(a.ref); const vis = !!(el && visible(el));
      ok = a.gone ? !vis : vis;
    } else {
      ok = document.readyState === 'complete';
    }
    return Object.assign(state(), { met: ok });
  }

  function waitFor(a) {
    a = a || {};
    const timeout = Math.max(100, Math.min(Number(a.timeout_ms) || 10000, 60000));
    const start = performance.now();
    const check = () => {
      if (a.text) {
        const has = textHas(a.text);
        return a.gone ? !has : has;
      }
      if (a.selector) {
        const el = queryAny(a.selector);
        const ok = !!(el && visible(el));
        return a.gone ? !ok : ok;
      }
      if (a.ref) { const el = byRef(a.ref); const ok = !!(el && visible(el)); return a.gone ? !ok : ok; }
      return document.readyState === 'complete';
    };
    return new Promise((resolve, reject) => {
      const tick = () => {
        let ok = false;
        try { ok = check(); } catch (e) { reject(e); return; }
        const waited = Math.round(performance.now() - start);
        if (ok) resolve(Object.assign(state(), { met: true, waited }));
        else if (waited >= timeout) resolve(Object.assign(state(), { met: false, waited }));
        else setTimeout(tick, 120);
      };
      tick();
    });
  }

  function elementAt(a) { const el = deepFromPoint(Number(a.x), Number(a.y)); return { label: el ? labelOf(el) : '空白处', ref: el ? refOf(el) : null }; }

  /** 直接在元素本身派发指针 / 鼠标事件并调用 click()，不经过坐标命中：
   *  用于目标被其它元素（如跨域框架）遮挡、而操作本身确认无误的情况。 */
  function activate(a) {
    a = a || {};
    const el = pick(a);
    if (!el) throw new Error('需要提供 ref、selector 或 text');
    if (!el.isConnected) throw new Error('目标元素已从页面移除，请重新执行 browser_snapshot');
    if (!visible(el)) throw new Error(labelOf(el) + ' 当前不可见（被隐藏或尺寸为 0）');
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') throw new Error(labelOf(el) + ' 处于禁用状态，点击不会生效');
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const w = winOf(el);
    const r = el.getBoundingClientRect();
    const base = { bubbles: true, cancelable: true, composed: true, view: w, button: 0,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const MouseCtor = w.MouseEvent;
    const PointerCtor = w.PointerEvent || MouseCtor;
    const fire = (Ctor, type, extra) => el.dispatchEvent(new Ctor(type, Object.assign({}, base, extra || {})));
    fire(PointerCtor, 'pointerover', { pointerType: 'mouse', isPrimary: true });
    fire(MouseCtor, 'mouseover');
    fire(PointerCtor, 'pointerdown', { pointerId: 1, pointerType: 'mouse', isPrimary: true, buttons: 1 });
    fire(MouseCtor, 'mousedown', { buttons: 1 });
    if (typeof el.focus === 'function') el.focus({ preventScroll: true });
    fire(PointerCtor, 'pointerup', { pointerId: 1, pointerType: 'mouse', isPrimary: true });
    fire(MouseCtor, 'mouseup');
    el.click();
    return { label: labelOf(el), ref: refOf(el), rect: rectOf(el) };
  }

  const FN = { snapshot, resolve, focus: focusTarget, selectAll, selection, cut: cutSelection, paste, fill, text: pageText,
    watch, quiet, state, check, scrollTo, scrollPos, wait: waitFor, elementAt, activate };

  window.__ccKit = {
    v: V,
    call(fn, args) {
      const f = FN[fn];
      if (!f) return { ok: false, error: '未知的页面工具：' + fn };
      try {
        const r = f(args || {});
        if (r && typeof r.then === 'function') return r.then((value) => ({ ok: true, value }), (e) => ({ ok: false, error: String(e && e.message || e) }));
        return { ok: true, value: r };
      } catch (e) {
        return { ok: false, error: String(e && e.message || e) };
      }
    },
  };
  return 'installed';
})()
""".replace("%VERSION%", str(KIT_VERSION))


def kit_call_expression(fn: str, args: dict | None = None) -> str:
    """生成「已安装则调用、未安装返回哨兵」的表达式（宿主据哨兵决定是否先注入 KIT_SOURCE）。"""
    import json

    payload = json.dumps(args or {}, ensure_ascii=False)
    return (
        f"(window.__ccKit && window.__ccKit.v === {KIT_VERSION})"
        f" ? window.__ccKit.call({json.dumps(fn)}, {payload}) : '__CC_NOKIT__'"
    )


NOKIT_SENTINEL = "__CC_NOKIT__"
