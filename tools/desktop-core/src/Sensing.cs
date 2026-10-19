// Sensing layer: window enumeration, UI Automation (TreeWalker), screen capture.
//
// Cost model that drives the design (measured on the target machine, physical px):
//   ElementFromPoint (+6 ancestors)   ~4.4 ms   -> 一次跨进程属性读取约 0.09 ms
//   TreeWalker snapshot, 50 nodes     ~20-90 ms (普通窗口) / ~400 ms (Chromium 深树)
//   FindAll(Descendants)              ~4400 ms on a Chromium window -> 禁用，见 Snapshot
//   full-screen capture+encode        ~110 ms
// Callers should therefore try the cheapest tier that can answer their question and only
// fall back to a screenshot when the control tree is empty (self-drawn UIs).
//
// All coordinates are PHYSICAL pixels: the process declares Per-Monitor-V2 awareness at
// startup, otherwise Windows virtualizes the coordinates and clicks land in the wrong spot.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Automation;

// 'Encoder' is ambiguous between System.Drawing.Imaging.Encoder and System.Text.Encoder
// once both namespaces are imported; alias the imaging one we actually need.
using Encoder = System.Drawing.Imaging.Encoder;

namespace DesktopCore
{
    internal sealed class WinInfo
    {
        public long Handle;
        public string Title = "";
        public string ClassName = "";
        public uint Pid;
        public string ProcessName = "";
        public int Left, Top, Width, Height;
        public bool Minimized;
        public bool Foreground;
    }

    internal static class Sensing
    {
        /// <summary>
        /// 快照遍历的默认深度上限。
        /// 网页/Electron 的 UIA 树可达数十层，而真正可交互的元素几乎都在前十几层；
        /// 不设上限等于放开整棵树遍历（实测 depth=16 时访问 4000 节点要 6.7 秒）。
        /// </summary>
        private const int DefaultDepthLimit = 12;

        /// <summary>
        /// 快照遍历的默认时间预算（毫秒）。到点即返回已收集的内容并标记 truncated。
        /// 这是响应时间的硬上界：网页树宽且深，深度与节点数都不足以约束实际耗时。
        /// </summary>
        private const int DefaultTimeBudgetMs = 800;

        public static IntPtr Foreground { get { return Native.GetForegroundWindow(); } }

        /// <summary>
        /// 取进程名（不含扩展名）。
        ///
        /// 为什么不用 System.Diagnostics.Process.GetProcessById：它会为每个 pid 打开句柄并
        /// 建立进程快照，窗口较多时（几十个不同 pid）累计让 windows op 花到 ~115 ms。
        /// QueryFullProcessImageNameW 只做一次轻量查询，实测把同一 op 降到十几毫秒。
        /// </summary>
        private static string ProcName(uint pid)
        {
            IntPtr h = Native.OpenProcess(Native.PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (h == IntPtr.Zero) return "";
            try
            {
                StringBuilder sb = new StringBuilder(1024);
                int size = sb.Capacity;
                if (!Native.QueryFullProcessImageNameW(h, 0, sb, ref size)) return "";
                string path = sb.ToString(0, size);
                int slash = path.LastIndexOf('\\');
                string name = slash >= 0 ? path.Substring(slash + 1) : path;
                if (name.EndsWith(".exe", System.StringComparison.OrdinalIgnoreCase))
                    name = name.Substring(0, name.Length - 4);
                return name;
            }
            catch { return ""; }
            finally { Native.CloseHandle(h); }
        }

        public static List<WinInfo> ListWindows(bool visibleOnly, int limit)
        {
            List<WinInfo> list = new List<WinInfo>();
            Dictionary<uint, string> names = new Dictionary<uint, string>();
            IntPtr fg = Native.GetForegroundWindow();

            Native.EnumWindows((h, lp) =>
            {
                if (list.Count >= limit) return false;
                if (visibleOnly && !Native.IsWindowVisible(h)) return true;

                StringBuilder title = new StringBuilder(512);
                Native.GetWindowTextW(h, title, 512);
                if (title.Length == 0) return true;

                StringBuilder cls = new StringBuilder(256);
                Native.GetClassNameW(h, cls, 256);

                uint pid;
                Native.GetWindowThreadProcessId(h, out pid);
                if (!names.ContainsKey(pid)) names[pid] = ProcName(pid);

                Native.RECT r;
                Native.GetWindowRect(h, out r);

                WinInfo w = new WinInfo();
                w.Handle = h.ToInt64();
                w.Title = title.ToString();
                w.ClassName = cls.ToString();
                w.Pid = pid;
                w.ProcessName = names[pid];
                w.Left = r.Left; w.Top = r.Top;
                w.Width = r.Right - r.Left; w.Height = r.Bottom - r.Top;
                w.Minimized = Native.IsIconic(h);
                w.Foreground = (h == fg);
                list.Add(w);
                return true;
            }, IntPtr.Zero);

            return list;
        }

        /// <summary>Resolve a window handle from an explicit handle or a title/process matcher.</summary>
        public static IntPtr Resolve(long handle, string title, string processName)
        {
            if (handle != 0) return new IntPtr(handle);
            if (string.IsNullOrEmpty(title) && string.IsNullOrEmpty(processName)) return IntPtr.Zero;

            IntPtr found = IntPtr.Zero;
            string t = (title ?? "").Trim();
            string p = (processName ?? "").Trim();

            Native.EnumWindows((h, lp) =>
            {
                if (!Native.IsWindowVisible(h)) return true;
                StringBuilder sb = new StringBuilder(512);
                Native.GetWindowTextW(h, sb, 512);
                string wt = sb.ToString();
                if (wt.Length == 0) return true;

                bool ok = true;
                if (t.Length > 0 && wt.IndexOf(t, StringComparison.OrdinalIgnoreCase) < 0) ok = false;
                if (ok && p.Length > 0)
                {
                    uint pid; Native.GetWindowThreadProcessId(h, out pid);
                    if (ProcName(pid).IndexOf(p, StringComparison.OrdinalIgnoreCase) < 0) ok = false;
                }
                if (ok) { found = h; return false; }
                return true;
            }, IntPtr.Zero);

            return found;
        }

        /// <summary>
        /// 窗口的**可见**边界矩形（plan-365-1757 A6）。
        ///
        /// 为什么不能直接用 GetWindowRect：它包含 DWM 的不可见调整边框（Win10/11 上约 7–8 px），
        /// 而 CDP 给的是视口 CSS 坐标、截图给的是真实像素——用含边框的矩形做换算基准会带来
        /// 与边框宽度成正比的系统性偏移，表现就是「网页元素点击总是差几个像素」。
        /// DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) 返回用户实际看到的矩形；
        /// 在极老系统或 DWM 关闭时它会失败，此时回退 GetWindowRect（不劣于改动前）。
        /// </summary>
        public static Native.RECT VisibleFrame(IntPtr h)
        {
            Native.RECT r = new Native.RECT();
            try
            {
                int size = Marshal.SizeOf(typeof(Native.RECT));
                if (Native.DwmGetWindowAttribute(h, Native.DWMWA_EXTENDED_FRAME_BOUNDS, out r, size) == 0
                    && r.Right > r.Left && r.Bottom > r.Top)
                    return r;
            }
            catch { /* DWM 不可用：走下面的回退 */ }
            Native.GetWindowRect(h, out r);
            return r;
        }

        /// <summary>Bring a window to the foreground and verify it actually became foreground.</summary>
        public static bool Focus(IntPtr h)
        {
            if (h == IntPtr.Zero) return false;
            if (Native.IsIconic(h)) Native.ShowWindow(h, Native.SW_RESTORE);
            if (Native.GetForegroundWindow() == h) return true;

            // Windows 前台锁定：非前台进程直接 SetForegroundWindow 会被系统静默忽略，
            // 实测表现就是「无法把该窗口激活到前台（可能被系统限制）」。
            // 标准解法：把本线程的输入队列附加到当前前台窗口的线程上（附加期间两者共享输入状态，
            // 前台锁定随之解除），置前完成后再解除附加。
            IntPtr fg = Native.GetForegroundWindow();
            uint fgPid;
            uint fgThread = fg == IntPtr.Zero ? 0 : Native.GetWindowThreadProcessId(fg, out fgPid);
            uint curThread = Native.GetCurrentThreadId();
            bool attached = false;
            if (fgThread != 0 && fgThread != curThread)
                attached = Native.AttachThreadInput(fgThread, curThread, true);
            try
            {
                Native.BringWindowToTop(h);
                Native.SetForegroundWindow(h);
                Native.SetActiveWindow(h);
                if (Native.IsIconic(h))
                {
                    Native.ShowWindow(h, Native.SW_RESTORE);
                    Native.SetForegroundWindow(h);
                }
            }
            finally
            {
                if (attached) Native.AttachThreadInput(fgThread, curThread, false);
            }

            for (int i = 0; i < 10; i++)
            {
                if (Native.GetForegroundWindow() == h) return true;
                System.Threading.Thread.Sleep(30);
            }
            return Native.GetForegroundWindow() == h;
        }

        public static string CtName(ControlType ct)
        {
            if (ct == null) return "";
            return ct.ProgrammaticName.Replace("ControlType.", "");
        }

        /// <summary>
        /// Batched subtree read.
        ///
        /// 为什么不用 FindAll（实测教训，两轮改写都栽在这里）：
        /// - `FindAll(TreeScope.Descendants)` 会先把整棵子树枚举完才返回，在 Chromium 窗口上
        ///   实测 4.4 秒（整棵树上万节点，而需要的交互元素只有几十个）；
        /// - 改成逐层 `FindAll(TreeScope.Children)` 后依然慢：单次调用 200–800 ms，
        ///   12 层下来仍要 3–4 秒。Chromium 的 provider 在 FindAll 上会做全子树代价，
        ///   与请求的 scope 无关。反复调用不会变快（已验证 6 次连续调用耗时持平），
        ///   所以不是「首次建树」的一次性成本。
        ///
        /// 现在改用 **TreeWalker 增量导航 + Current 属性直读**：
        /// ElementFromPoint 带 6 层祖先链（≈49 次属性读取）实测仅 4.4 ms，
        /// 即一次跨进程调用约 0.09 ms。按同一成本估算，50 个节点 × 7 个属性 ≈ 30 ms，
        /// 比 FindAll 快两个数量级，且耗时随输出规模线性可控。
        /// 注意：TreeWalker 返回的元素**不支持 Cached**（会抛异常，表现为永远 0 个元素），
        /// 必须读 `.Current`。
        /// </summary>
        public static string SnapshotWalk(IntPtr root, int budget, bool interactiveOnly, int depthLimit,
                                          int timeBudgetMs)
        {
            if (budget <= 0) budget = 200;
            // 网页/Electron 的 UIA 树可达数十层；不设上限等于放开整棵树遍历。
            if (depthLimit <= 0) depthLimit = DefaultDepthLimit;
            // 访问总量天花板：保证遇到异常窗口也只慢一次、不会挂住。
            int maxVisited = Math.Max(2000, budget * 20);
            // 真正的响应时间护栏：实测每访问一个节点约 1.7–3.8 ms（Chromium 的 provider
            // 每次属性读取都是一次跨进程调用）。仅靠节点数上限不够——实测 depth=16 时
            // 4000 个节点要 6.7 秒。改为按墙钟时间截断，用户可感知的延迟因此有上界。
            if (timeBudgetMs <= 0) timeBudgetMs = DefaultTimeBudgetMs;

            AutomationElement el = AutomationElement.FromHandle(root);
            if (el == null)
                return "{\"nodes\":0,\"items\":[],\"visited\":0,\"truncated\":false}";

            StringBuilder sb = new StringBuilder(16384);
            int n = 0;
            int visited = 0;
            bool truncated = false;
            System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();

            TreeWalker walker = TreeWalker.ControlViewWalker;
            // 广度优先：浅层元素优先出现（对模型更有用），且便于同时施加深度与总量上限。
            // 遍历与输出必须解耦——容器节点（无名称、不可交互）不进输出但必须继续下钻，
            // 否则在「根 → 一堆容器 → 真正的控件」结构上会立刻剪枝成 0 个元素。
            Queue<KeyValuePair<AutomationElement, int>> queue =
                new Queue<KeyValuePair<AutomationElement, int>>();
            queue.Enqueue(new KeyValuePair<AutomationElement, int>(el, 0));

            while (queue.Count > 0 && n < budget)
            {
                if (visited >= maxVisited || sw.ElapsedMilliseconds > timeBudgetMs)
                {
                    truncated = true;
                    break;
                }
                KeyValuePair<AutomationElement, int> cur = queue.Dequeue();
                AutomationElement parent = cur.Key;
                int depth = cur.Value;
                int childDepth = depth + 1;

                AutomationElement child;
                try { child = walker.GetFirstChild(parent); } catch { child = null; }
                while (child != null)
                {
                    if (visited >= maxVisited || n >= budget
                        || sw.ElapsedMilliseconds > timeBudgetMs)
                    {
                        truncated = true;
                        break;
                    }
                    visited++;

                    // 深度未到上限就继续下钻（与是否输出无关）
                    if (childDepth < depthLimit)
                    {
                        queue.Enqueue(new KeyValuePair<AutomationElement, int>(child, childDepth));
                    }

                    string name = "", id = "", cls = "", ct = "";
                    bool enabled = false, offscreen = true, focusable = false;
                    System.Windows.Rect r = new System.Windows.Rect();
                    bool readable = true;
                    try
                    {
                        name = child.Current.Name ?? "";
                        id = child.Current.AutomationId ?? "";
                        cls = child.Current.ClassName ?? "";
                        ct = CtName(child.Current.ControlType);
                        enabled = child.Current.IsEnabled;
                        offscreen = child.Current.IsOffscreen;
                        focusable = child.Current.IsKeyboardFocusable;
                        r = child.Current.BoundingRectangle;
                    }
                    catch { readable = false; }

                    if (readable && !offscreen)
                    {
                        bool interactive = focusable || IsInteractiveCt(ct);
                        // Skip container noise that carries no label and no geometry.
                        if (!(interactiveOnly && !interactive) && (interactive || name.Length > 0))
                        {
                            if (n > 0) sb.Append(',');
                            sb.Append("{\"n\":\"").Append(Js.Esc(name))
                              .Append("\",\"id\":\"").Append(Js.Esc(id))
                              .Append("\",\"ct\":\"").Append(Js.Esc(ct))
                              .Append("\",\"cls\":\"").Append(Js.Esc(cls))
                              .Append("\",\"en\":").Append(enabled ? "true" : "false")
                              .Append(",\"ix\":").Append(interactive ? "true" : "false")
                              // Centre point, ready for a click without further arithmetic.
                              .Append(",\"x\":").Append((int)(r.Left + r.Width / 2))
                              .Append(",\"y\":").Append((int)(r.Top + r.Height / 2))
                              .Append(",\"r\":[").Append((int)r.Left).Append(',').Append((int)r.Top)
                              .Append(',').Append((int)r.Width).Append(',').Append((int)r.Height).Append("]}");
                            n++;
                        }
                    }

                    try { child = walker.GetNextSibling(child); } catch { child = null; }
                }
            }

            return "{\"nodes\":" + n + ",\"items\":[" + sb + "],\"visited\":" + visited
                 + ",\"truncated\":" + (truncated ? "true" : "false") + ",\"path\":\"walk\"}";
        }

        // ── 快速快照（plan-20261019-desktop-targeting）────────────────────────────
        //
        // 为什么重写：旧实现用 TreeWalker 逐节点读 .Current，Chrome 上每节点 1–4 ms，而且只从窗口顶层向下 12 层，
        // 网页内容（Document 在第 6 层，页面 DOM 又嵌套十几层）基本进不了结果；叠加 Chromium 的 UIA 树是「惰性」的——
        // 首次访问才开始构建——一次性快照常只看到标题栏按钮（README 与 diag-chrome-uia.ps1 实测的现象），
        // 于是浏览器里的网页被判定为「读不到控件」，只能靠模型看图估坐标。
        //
        // 本机实测（Chrome，150% 缩放）：
        //   · 首次访问后约 0.2–0.5 s 网页树就绪，之后一直保持（同一窗口无需再预热）；
        //   · FindAll(Descendants) 配 CacheRequest 并带 IsOffscreen=false 条件：Chromium 的 provider 在服务端过滤，
        //     7000 节点的页面只回传屏幕内的 168 个，耗时 125 ms（无条件 FindAll 要 1.3 s，逐节点遍历要 2 s）；
        //   · UIA 给的矩形就是物理像素，与截图、点击同一坐标系，不需要任何换算。
        // 因此：Chromium 窗口先预热、再一次性取「屏幕内可见节点」，网页元素与原生控件一样可按编号点击。

        /// <summary>Chromium / CEF / Gecko 窗口类名前缀：命中说明 UIA 网页树是惰性构建的，首次访问后需要预热。</summary>
        private static readonly string[] ChromiumClassPrefixes = new string[] {
            "Chrome_WidgetWin_", "CefWebViewWnd", "Chrome_RenderWidgetHostHWND", "MozillaWindowClass" };

        public static bool IsChromiumLike(IntPtr h)
        {
            StringBuilder cls = new StringBuilder(256);
            Native.GetClassNameW(h, cls, 256);
            string c = cls.ToString();
            foreach (string p in ChromiumClassPrefixes)
                if (c.StartsWith(p, StringComparison.Ordinal)) return true;
            return false;
        }

        // 已预热窗口：Chromium 的无障碍模式一旦打开会保持到进程结束，同一 hwnd 之后不必再等
        private static readonly Dictionary<long, long> WarmWindows = new Dictionary<long, long>();

        private static bool IsWarm(IntPtr h)
        {
            lock (WarmWindows) { return WarmWindows.ContainsKey(h.ToInt64()); }
        }

        private static void MarkWarm(IntPtr h)
        {
            lock (WarmWindows)
            {
                if (WarmWindows.Count > 256) WarmWindows.Clear();
                WarmWindows[h.ToInt64()] = DateTime.UtcNow.Ticks;
            }
        }

        /// <summary>
        /// 后台触发 Chromium 窗口的网页无障碍树构建（focus 时顺手调用）：等模型真正取元素时树已经就绪，
        /// 省掉首次读取的等待。只对 Chromium 系窗口、且只触发一次。
        /// </summary>
        public static void WarmAsync(IntPtr h)
        {
            if (h == IntPtr.Zero || IsWarm(h) || !IsChromiumLike(h)) return;
            System.Threading.Thread t = new System.Threading.Thread(delegate()
            {
                try
                {
                    AutomationElement top = AutomationElement.FromHandle(h);
                    top.FindFirst(TreeScope.Descendants,
                        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document));
                }
                catch { /* 预热失败不影响后续的正式读取 */ }
            });
            t.IsBackground = true;
            t.SetApartmentState(System.Threading.ApartmentState.MTA);
            t.Start();
        }

        /// <summary>一个可见节点的缓存属性快照（读完即与 UIA 解耦）。</summary>
        private sealed class SnapItem
        {
            public int Index;
            public string Name = "", Id = "", Cls = "", Ct = "";
            public bool Enabled, Focusable, Interactive;
            public double L, T, W, H;
            public bool InDoc;
        }

        private static bool IsContainerCt(string ct)
        {
            switch (ct)
            {
                case "Document": case "Window": case "Pane": case "TitleBar": case "ScrollBar": case "ToolBar":
                    return true;
                default:
                    return false;
            }
        }

        /// <summary>在工作线程里做一次「屏幕内可见节点」查询；超时返回 null 并置 timedOut。</summary>
        private static List<SnapItem> FastCollect(IntPtr root, int timeBudgetMs, out bool timedOut)
        {
            timedOut = false;
            List<SnapItem> result = null;
            System.Threading.Thread t = new System.Threading.Thread(delegate()
            {
                try { result = FastCollectCore(root); }
                catch { result = null; }
            });
            t.IsBackground = true;
            t.SetApartmentState(System.Threading.ApartmentState.MTA);
            t.Start();
            if (!t.Join(Math.Max(100, timeBudgetMs)))
            {
                // COM 调用无法取消：线程留在后台自行结束，本次放弃
                timedOut = true;
                return null;
            }
            return result;
        }

        private static List<SnapItem> FastCollectCore(IntPtr root)
        {
            AutomationElement top = AutomationElement.FromHandle(root);
            if (top == null) return new List<SnapItem>();

            CacheRequest cr = new CacheRequest();
            cr.Add(AutomationElement.NameProperty);
            cr.Add(AutomationElement.AutomationIdProperty);
            cr.Add(AutomationElement.ClassNameProperty);
            cr.Add(AutomationElement.ControlTypeProperty);
            cr.Add(AutomationElement.IsEnabledProperty);
            cr.Add(AutomationElement.IsKeyboardFocusableProperty);
            cr.Add(AutomationElement.BoundingRectangleProperty);
            cr.TreeScope = TreeScope.Element;
            // 只要缓存值、不要活的元素引用：节点多时明显更省
            cr.AutomationElementMode = AutomationElementMode.None;

            Condition onScreen = new PropertyCondition(AutomationElement.IsOffscreenProperty, false);
            List<SnapItem> list = new List<SnapItem>();
            using (cr.Activate())
            {
                AutomationElementCollection col = top.FindAll(TreeScope.Descendants, onScreen);
                for (int i = 0; i < col.Count; i++)
                {
                    try
                    {
                        AutomationElement e = col[i];
                        System.Windows.Rect r = e.Cached.BoundingRectangle;
                        if (r.IsEmpty || double.IsInfinity(r.Width) || double.IsInfinity(r.Height)) continue;
                        SnapItem s = new SnapItem();
                        s.Index = i;
                        s.Name = (e.Cached.Name ?? "").Trim();
                        s.Id = e.Cached.AutomationId ?? "";
                        s.Cls = e.Cached.ClassName ?? "";
                        s.Ct = CtName(e.Cached.ControlType);
                        s.Enabled = e.Cached.IsEnabled;
                        s.Focusable = e.Cached.IsKeyboardFocusable;
                        s.L = r.Left; s.T = r.Top; s.W = r.Width; s.H = r.Height;
                        s.Interactive = IsInteractiveCt(s.Ct) || (s.Focusable && !IsContainerCt(s.Ct));
                        list.Add(s);
                    }
                    catch { /* 单个节点读取失败（元素已消失）直接跳过 */ }
                }
            }
            return list;
        }

        /// <summary>找出页面 Document（取面积最大的一个）；没有返回 null。</summary>
        private static SnapItem FindDocument(List<SnapItem> items)
        {
            SnapItem best = null;
            foreach (SnapItem s in items)
            {
                if (s.Ct != "Document" || s.W <= 0 || s.H <= 0) continue;
                if (best == null || s.W * s.H > best.W * best.H) best = s;
            }
            return best;
        }

        private static bool CenterInside(SnapItem s, SnapItem doc)
        {
            double cx = s.L + s.W / 2, cy = s.T + s.H / 2;
            return cx >= doc.L && cx <= doc.L + doc.W && cy >= doc.T && cy <= doc.T + doc.H;
        }

        /// <summary>
        /// 快照入口：Chromium 窗口先预热（最多 warmupMs），再一次性取屏幕内可见节点；
        /// 查询失败或超时时回退到 TreeWalker 逐层遍历（旧实现），保证最差情况不劣于改动前。
        /// </summary>
        public static string Snapshot(IntPtr root, int budget, bool interactiveOnly, int depthLimit,
                                      int timeBudgetMs, int warmupMs)
        {
            if (budget <= 0) budget = 200;
            if (timeBudgetMs <= 0) timeBudgetMs = DefaultTimeBudgetMs;
            bool chromium = IsChromiumLike(root);
            bool needWarm = chromium && warmupMs > 0 && !IsWarm(root);

            System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
            List<SnapItem> items = null;
            bool timedOut = false;
            bool warmed = false;
            SnapItem doc = null;
            while (true)
            {
                items = FastCollect(root, timeBudgetMs, out timedOut);
                if (items == null) break;
                doc = FindDocument(items);
                if (!needWarm) break;
                // 网页树就绪的判据：Document 在屏幕上，且它的范围内已经有节点
                int inside = 0;
                if (doc != null)
                    foreach (SnapItem s in items)
                        if (s != doc && CenterInside(s, doc)) { inside++; if (inside > 0) break; }
                if (doc != null && inside > 0) { warmed = true; MarkWarm(root); break; }
                if (sw.ElapsedMilliseconds >= warmupMs)
                {
                    // 超时也记下：空白页 / 无网页内容的窗口不必每次都等满
                    MarkWarm(root);
                    break;
                }
                // 连 Document 都没出现：多半是应用关闭了渲染进程的无障碍（如部分 Electron 套壳），
                // 实测 0.9 s 内不出现就不会出现——不必为它等满预热上限
                if (doc == null && sw.ElapsedMilliseconds >= Math.Min(warmupMs, 900))
                {
                    MarkWarm(root);
                    break;
                }
                System.Threading.Thread.Sleep(120);
            }

            if (items == null)
            {
                // 回退：老办法（有时间预算，最差也只多花一个预算）
                string walked = SnapshotWalk(root, budget, interactiveOnly, depthLimit,
                                             Math.Max(300, timeBudgetMs / 2));
                int cut = walked.LastIndexOf('}');
                return (cut > 0 ? walked.Substring(0, cut) : walked)
                     + ",\"chromium\":" + (chromium ? "true" : "false")
                     + ",\"fallback\":\"" + (timedOut ? "timeout" : "error") + "\"}";
            }

            // 筛选：有矩形；有名称或可交互；容器类（Document/Pane/Window…）本身不列
            List<SnapItem> keep = new List<SnapItem>();
            foreach (SnapItem s in items)
            {
                if (s.W < 1 || s.H < 1) continue;
                if (IsContainerCt(s.Ct)) continue;
                if (s.Name.Length == 0 && !s.Interactive) continue;
                if (interactiveOnly && !s.Interactive) continue;
                if (doc != null) s.InDoc = CenterInside(s, doc);
                keep.Add(s);
            }
            int total = keep.Count;
            bool truncated = false;
            if (keep.Count > budget)
            {
                // 超预算时优先保留可交互元素，其余用有名称的文字补满，输出仍按原树序
                truncated = true;
                List<SnapItem> chosen = new List<SnapItem>();
                foreach (SnapItem s in keep) if (s.Interactive && chosen.Count < budget) chosen.Add(s);
                foreach (SnapItem s in keep) if (!s.Interactive && chosen.Count < budget) chosen.Add(s);
                chosen.Sort(delegate(SnapItem a, SnapItem b) { return a.Index.CompareTo(b.Index); });
                keep = chosen;
            }

            StringBuilder sb = new StringBuilder(16384);
            for (int i = 0; i < keep.Count; i++)
            {
                SnapItem s = keep[i];
                if (i > 0) sb.Append(',');
                sb.Append("{\"n\":\"").Append(Js.Esc(s.Name))
                  .Append("\",\"id\":\"").Append(Js.Esc(s.Id))
                  .Append("\",\"ct\":\"").Append(Js.Esc(s.Ct))
                  .Append("\",\"cls\":\"").Append(Js.Esc(s.Cls))
                  .Append("\",\"en\":").Append(s.Enabled ? "true" : "false")
                  .Append(",\"ix\":").Append(s.Interactive ? "true" : "false")
                  .Append(",\"d\":").Append(s.InDoc ? "1" : "0")
                  .Append(",\"x\":").Append((int)(s.L + s.W / 2))
                  .Append(",\"y\":").Append((int)(s.T + s.H / 2))
                  .Append(",\"r\":[").Append((int)s.L).Append(',').Append((int)s.T)
                  .Append(',').Append((int)s.W).Append(',').Append((int)s.H).Append("]}");
            }
            string docJson = doc == null ? "null"
                : "[" + (int)doc.L + "," + (int)doc.T + "," + (int)doc.W + "," + (int)doc.H + "]";
            return "{\"nodes\":" + keep.Count + ",\"items\":[" + sb + "],\"visited\":" + items.Count
                 + ",\"total\":" + total
                 + ",\"truncated\":" + (truncated ? "true" : "false")
                 + ",\"path\":\"findall\",\"chromium\":" + (chromium ? "true" : "false")
                 + ",\"warmed\":" + (warmed ? "true" : "false")
                 + ",\"warmMs\":" + sw.ElapsedMilliseconds
                 + ",\"doc\":" + docJson + "}";
        }

        /// <summary>
        /// 点击点下的元素简述（点击前取，约 4 ms）：返回 {n,id,ct,cls,ix,r} 的 JSON 对象文本；
        /// 取不到或超时返回 null。放到工作线程里并设上限——对话框卡死的应用上 UIA 调用可能阻塞很久，
        /// 不能让它拖住整个内核。
        /// </summary>
        public static string HitBrief(int x, int y, int timeoutMs)
        {
            string res = null;
            System.Threading.Thread t = new System.Threading.Thread(delegate()
            {
                try
                {
                    AutomationElement e = AutomationElement.FromPoint(new System.Windows.Point(x, y));
                    if (e == null) return;
                    string name = e.Current.Name ?? "";
                    string id = e.Current.AutomationId ?? "";
                    string cls = e.Current.ClassName ?? "";
                    string ct = CtName(e.Current.ControlType);
                    bool focusable = e.Current.IsKeyboardFocusable;
                    System.Windows.Rect r = e.Current.BoundingRectangle;
                    bool ix = IsInteractiveCt(ct) || (focusable && !IsContainerCt(ct));
                    StringBuilder sb = new StringBuilder(256);
                    sb.Append("{\"n\":\"").Append(Js.Esc(name.Length > 80 ? name.Substring(0, 80) : name))
                      .Append("\",\"id\":\"").Append(Js.Esc(id))
                      .Append("\",\"ct\":\"").Append(Js.Esc(ct))
                      .Append("\",\"cls\":\"").Append(Js.Esc(cls))
                      .Append("\",\"ix\":").Append(ix ? "true" : "false");
                    if (!r.IsEmpty && !double.IsInfinity(r.Width))
                        sb.Append(",\"r\":[").Append((int)r.Left).Append(',').Append((int)r.Top)
                          .Append(',').Append((int)r.Width).Append(',').Append((int)r.Height).Append(']');
                    sb.Append('}');
                    res = sb.ToString();
                }
                catch { res = null; }
            });
            t.IsBackground = true;
            t.SetApartmentState(System.Threading.ApartmentState.MTA);
            t.Start();
            if (!t.Join(Math.Max(50, timeoutMs))) return null;
            return res;
        }

        private static bool IsInteractiveCt(string ct)
        {
            switch (ct)
            {
                case "Button": case "Edit": case "ComboBox": case "CheckBox": case "RadioButton":
                case "Hyperlink": case "ListItem": case "MenuItem": case "TabItem": case "TreeItem":
                case "SplitButton": case "Slider": case "Spinner": case "DataItem": case "HeaderItem":
                    return true;
                default:
                    return false;
            }
        }

        /// <summary>Cheapest possible hit test: what element sits at this screen point?</summary>
        public static string HitAt(int x, int y, bool withAncestors)
        {
            AutomationElement e = AutomationElement.FromPoint(new System.Windows.Point(x, y));
            if (e == null) return "{\"found\":false}";

            StringBuilder sb = new StringBuilder(512);
            sb.Append("{\"found\":true");
            AppendElement(sb, e);

            if (withAncestors)
            {
                // Walking up gives the model "button inside toolbar inside window" context
                // for a single 2.5 ms call, which is far cheaper than a full tree walk.
                sb.Append(",\"chain\":[");
                TreeWalker walker = TreeWalker.ControlViewWalker;
                AutomationElement p = walker.GetParent(e);
                int depth = 0;
                while (p != null && depth < 6)
                {
                    if (depth > 0) sb.Append(',');
                    sb.Append('{');
                    AppendElementBody(sb, p);
                    sb.Append('}');
                    p = walker.GetParent(p);
                    depth++;
                }
                sb.Append(']');
            }
            sb.Append('}');
            return sb.ToString();
        }

        private static void AppendElementBody(StringBuilder sb, AutomationElement e)
        {
            try
            {
                System.Windows.Rect r = e.Current.BoundingRectangle;
                sb.Append("\"n\":\"").Append(Js.Esc(e.Current.Name ?? ""))
                  .Append("\",\"id\":\"").Append(Js.Esc(e.Current.AutomationId ?? ""))
                  .Append("\",\"ct\":\"").Append(Js.Esc(CtName(e.Current.ControlType)))
                  .Append("\",\"cls\":\"").Append(Js.Esc(e.Current.ClassName ?? ""))
                  .Append("\",\"en\":").Append(e.Current.IsEnabled ? "true" : "false")
                  .Append(",\"x\":").Append((int)(r.Left + r.Width / 2))
                  .Append(",\"y\":").Append((int)(r.Top + r.Height / 2))
                  .Append(",\"r\":[").Append((int)r.Left).Append(',').Append((int)r.Top)
                  .Append(',').Append((int)r.Width).Append(',').Append((int)r.Height).Append(']');
            }
            catch { sb.Append("\"n\":\"\""); }
        }

        private static void AppendElement(StringBuilder sb, AutomationElement e)
        {
            sb.Append(',');
            AppendElementBody(sb, e);
        }

        // ── 画面采样与稳定检测（plan-20261019-desktop-precision）──────────────────
        //
        // 旧实现（plan-365-1757 A7）按「两次抓图逐字节相同」判稳定：输入框光标闪烁、悬浮窗小动画、
        // 任务栏时钟都会让它永远判为不稳定，每次等满超时（实测连续 5 次 3 s 超时）。
        // 改为缩略灰度图 + 容差：变化像素占比低于阈值即视为同一画面，噪声级的局部变化不再阻塞。

        /// <summary>区域缩略采样的长边像素：足以分辨界面级变化，单次采样只需几毫秒。</summary>
        private const int ThumbSide = 480;
        /// <summary>单个像素灰度差超过此值才算变化：滤掉缩放与抗锯齿带来的轻微抖动。</summary>
        private const int PixelDelta = 18;
        /// <summary>相邻两帧变化占比不超过此值视为静止（光标闪烁、悬浮窗小动画都在此之下）。</summary>
        private const double StillRatio = 0.003;
        /// <summary>动作前后变化占比超过此值视为画面有变化。</summary>
        private const double ChangedRatio = 0.003;
        /// <summary>动作点周边局部块的边长（物理像素）与判定「局部有变化」的最少变化像素数。</summary>
        private const int LocalSide = 160;
        private const int LocalMinChanged = 6;

        /// <summary>动作前的画面采样：区域缩略图 + 动作点周边局部块（无动作点时为 null）。</summary>
        internal sealed class Sample
        {
            public byte[] Global;
            public byte[] Local;
        }

        /// <summary>把屏幕矩形缩略成灰度数组（长边不超过 maxSide）；失败返回 null。</summary>
        public static byte[] Thumb(int x, int y, int w, int h, int maxSide)
        {
            if (w <= 0 || h <= 0) return null;
            double sc = Math.Min(1.0, (double)maxSide / Math.Max(w, h));
            int tw = Math.Max(1, (int)Math.Round(w * sc));
            int th = Math.Max(1, (int)Math.Round(h * sc));

            IntPtr screen = Native.GetDC(IntPtr.Zero);
            if (screen == IntPtr.Zero) return null;
            IntPtr mem = IntPtr.Zero, dib = IntPtr.Zero, old = IntPtr.Zero;
            try
            {
                mem = Native.CreateCompatibleDC(screen);
                Native.BITMAPINFO bmi = new Native.BITMAPINFO();
                bmi.bmiHeader.biSize = (uint)Marshal.SizeOf(typeof(Native.BITMAPINFOHEADER));
                bmi.bmiHeader.biWidth = tw;
                bmi.bmiHeader.biHeight = -th;   // 负高度 = 自上而下的行序
                bmi.bmiHeader.biPlanes = 1;
                bmi.bmiHeader.biBitCount = 32;
                IntPtr bits;
                dib = Native.CreateDIBSection(mem, ref bmi, 0, out bits, IntPtr.Zero, 0);
                if (dib == IntPtr.Zero || bits == IntPtr.Zero) return null;
                old = Native.SelectObject(mem, dib);
                // HALFTONE 按区域平均缩小，比最近邻稳定得多：同一画面两次采样几乎逐像素一致
                Native.SetStretchBltMode(mem, Native.HALFTONE);
                Native.SetBrushOrgEx(mem, 0, 0, IntPtr.Zero);
                if (!Native.StretchBlt(mem, 0, 0, tw, th, screen, x, y, w, h, Native.SRCCOPY)) return null;
                Native.GdiFlush();

                int[] px = new int[tw * th];
                Marshal.Copy(bits, px, 0, px.Length);
                byte[] gray = new byte[px.Length];
                for (int i = 0; i < px.Length; i++)
                {
                    int v = px[i];
                    int b = v & 0xFF, g = (v >> 8) & 0xFF, r = (v >> 16) & 0xFF;
                    gray[i] = (byte)((r * 299 + g * 587 + b * 114) / 1000);
                }
                return gray;
            }
            catch { return null; }
            finally
            {
                if (old != IntPtr.Zero) Native.SelectObject(mem, old);
                if (dib != IntPtr.Zero) Native.DeleteObject(dib);
                if (mem != IntPtr.Zero) Native.DeleteDC(mem);
                Native.ReleaseDC(IntPtr.Zero, screen);
            }
        }

        /// <summary>区域缺省（宽或高 ≤ 0）时取整个虚拟桌面。</summary>
        private static void NormalizeRegion(ref int x, ref int y, ref int w, ref int h)
        {
            if (w > 0 && h > 0) return;
            x = Native.GetSystemMetrics(Native.SM_XVIRTUALSCREEN);
            y = Native.GetSystemMetrics(Native.SM_YVIRTUALSCREEN);
            w = Native.GetSystemMetrics(Native.SM_CXVIRTUALSCREEN);
            h = Native.GetSystemMetrics(Native.SM_CYVIRTUALSCREEN);
            if (w <= 0) w = Native.GetSystemMetrics(Native.SM_CXSCREEN);
            if (h <= 0) h = Native.GetSystemMetrics(Native.SM_CYSCREEN);
        }

        /// <summary>两份采样的变化像素占比；尺寸不一致或缺失视为完全不同（changed = int.MaxValue）。</summary>
        private static double ChangeRatio(byte[] a, byte[] b, out int changed)
        {
            changed = 0;
            if (a == null || b == null || a.Length == 0 || a.Length != b.Length)
            {
                changed = int.MaxValue;
                return 1.0;
            }
            for (int i = 0; i < a.Length; i++)
            {
                int d = a[i] - b[i];
                if (d < 0) d = -d;
                if (d > PixelDelta) changed++;
            }
            return (double)changed / a.Length;
        }

        private static byte[] LocalThumb(int px, int py)
        {
            int half = LocalSide / 2;
            return Thumb(px - half, py - half, LocalSide, LocalSide, LocalSide);
        }

        /// <summary>动作前取样；调用方应在鼠标已移到目标、悬停态生效之后再取，悬停效果不计入动作引起的变化。</summary>
        public static Sample TakeSample(int x, int y, int w, int h, bool hasPoint, int px, int py)
        {
            NormalizeRegion(ref x, ref y, ref w, ref h);
            Sample s = new Sample();
            s.Global = Thumb(x, y, w, h, ThumbSide);
            if (hasPoint) s.Local = LocalThumb(px, py);
            return s;
        }

        /// <summary>与动作前相比是否有可见变化：区域缩略图占比，或动作点周边局部块（捕捉勾选框、焦点框这类小变化）。</summary>
        private static bool HasChanged(Sample before, byte[] global, bool hasPoint, int px, int py)
        {
            int n;
            if (before.Global != null && global != null
                && ChangeRatio(before.Global, global, out n) > ChangedRatio)
                return true;
            if (hasPoint && before.Local != null)
            {
                ChangeRatio(before.Local, LocalThumb(px, py), out n);
                if (n != int.MaxValue && n >= LocalMinChanged) return true;
            }
            return false;
        }

        /// <summary>
        /// 等待区域画面稳定：先等 minMs，再每 intervalMs 采样一次，相邻两次比较都静止即视为稳定，最长 timeoutMs。
        ///
        /// before 非空时同时判定动作是否引起了可见变化（返回 changed）：已静止但还没出现任何变化时，
        /// 继续等到 noChangeMs——先发请求再渲染的界面常在 300–800 ms 后才开始变，过早截图正是
        /// 「图上还是旧画面 → 模型再点一次」的根源（plan-20261019-desktop-precision 实录）。
        /// </summary>
        public static string Settle(int x, int y, int w, int h, Sample before, bool hasPoint, int px, int py,
                                    int minMs, int timeoutMs, int intervalMs, int noChangeMs)
        {
            NormalizeRegion(ref x, ref y, ref w, ref h);
            if (minMs < 0) minMs = 0;
            if (timeoutMs <= 0) timeoutMs = 1500;
            if (intervalMs <= 0) intervalMs = 60;
            if (noChangeMs <= 0) noChangeMs = 900;
            // 动作前采样失败时无从判断有无变化：只判稳定，不给出 changed
            bool judge = before != null && (before.Global != null || before.Local != null);

            System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
            if (minMs > 0) System.Threading.Thread.Sleep(minMs);
            byte[] prev = Thumb(x, y, w, h, ThumbSide);
            int checks = 1, stillRuns = 0, changedPx;
            bool sawChange = judge && HasChanged(before, prev, hasPoint, px, py);
            bool stable = false;
            while (sw.ElapsedMilliseconds < timeoutMs)
            {
                System.Threading.Thread.Sleep(intervalMs);
                byte[] cur = Thumb(x, y, w, h, ThumbSide);
                checks++;
                if (judge && !sawChange) sawChange = HasChanged(before, cur, hasPoint, px, py);
                double r = ChangeRatio(prev, cur, out changedPx);
                stillRuns = r <= StillRatio ? stillRuns + 1 : 0;
                prev = cur;
                if (stillRuns >= 2 && (!judge || sawChange || sw.ElapsedMilliseconds >= noChangeMs))
                {
                    stable = true;
                    break;
                }
            }

            return "{\"stable\":" + (stable ? "true" : "false")
                 + ",\"elapsedMs\":" + sw.ElapsedMilliseconds
                 + ",\"checks\":" + checks
                 + (judge ? ",\"changed\":" + (sawChange ? "true" : "false") : "")
                 + "}";
        }

        /// <summary>兼容旧 op（waitstable）：只判稳定，口径与 Settle 一致。</summary>
        public static string WaitStable(int x, int y, int w, int h, int timeoutMs, int settleMs)
        {
            return Settle(x, y, w, h, null, false, 0, 0, 0, timeoutMs <= 0 ? 3000 : timeoutMs,
                          settleMs <= 0 ? 60 : settleMs, 0);
        }

        /// <summary>
        /// Screen capture. GDI BitBlt into a compatible bitmap measures ~33 ms for
        /// 2560x1600; JPEG encoding is separate because it dominates (~30 ms) and callers
        /// may want raw pixels.
        /// </summary>
        public static string Capture(string outPath, int x, int y, int w, int h, int maxDim,
                                      string format, int quality)
        {
            return Capture(outPath, x, y, w, h, maxDim, format, quality, false);
        }

        /// <summary>
        /// fit=true 时小区域也放大到长边 maxDim（最多 4 倍，plan-20261019-desktop-precision）：
        /// 「放大查看」的意义在于让模型看清小字与图标，原尺寸裁剪对很小的区域几乎没有帮助。
        /// </summary>
        public static string Capture(string outPath, int x, int y, int w, int h, int maxDim,
                                      string format, int quality, bool fit)
        {
            int vx = Native.GetSystemMetrics(Native.SM_XVIRTUALSCREEN);
            int vy = Native.GetSystemMetrics(Native.SM_YVIRTUALSCREEN);
            int vw = Native.GetSystemMetrics(Native.SM_CXVIRTUALSCREEN);
            int vh = Native.GetSystemMetrics(Native.SM_CYVIRTUALSCREEN);
            if (vw <= 0) vw = Native.GetSystemMetrics(Native.SM_CXSCREEN);
            if (vh <= 0) vh = Native.GetSystemMetrics(Native.SM_CYSCREEN);

            if (w <= 0 || h <= 0)
            {
                x = vx; y = vy; w = vw; h = vh;
            }

            Bitmap bmp = new Bitmap(w, h, PixelFormat.Format24bppRgb);
            using (Graphics g = Graphics.FromImage(bmp))
            {
                IntPtr sdc = Native.GetDC(IntPtr.Zero);
                IntPtr mdc = Native.CreateCompatibleDC(sdc);
                try
                {
                    g.CopyFromScreen(x, y, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
                }
                finally
                {
                    Native.DeleteDC(mdc);
                    Native.ReleaseDC(IntPtr.Zero, sdc);
                }
            }

            Bitmap output = bmp;
            Bitmap scaled = null;
            int ow = w, oh = h;
            bool shrink = maxDim > 0 && (w > maxDim || h > maxDim);
            bool grow = fit && maxDim > 0 && w < maxDim && h < maxDim;
            if (shrink || grow)
            {
                double sc = Math.Min((double)maxDim / w, (double)maxDim / h);
                if (grow) sc = Math.Min(sc, 4.0);
                ow = Math.Max(1, (int)Math.Round(w * sc));
                oh = Math.Max(1, (int)Math.Round(h * sc));
                scaled = new Bitmap(ow, oh, PixelFormat.Format24bppRgb);
                using (Graphics g2 = Graphics.FromImage(scaled))
                {
                    // 缩小用双线性（实测比双三次快，缩小后肉眼无差别）；放大用双三次，小字边缘更清楚
                    g2.InterpolationMode = grow
                        ? System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic
                        : System.Drawing.Drawing2D.InterpolationMode.HighQualityBilinear;
                    g2.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.HighQuality;
                    g2.DrawImage(bmp, 0, 0, ow, oh);
                }
                output = scaled;
            }

            if (string.IsNullOrEmpty(outPath))
                outPath = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "desktop_core_shot.jpg");

            string dir = System.IO.Path.GetDirectoryName(outPath);
            if (!string.IsNullOrEmpty(dir) && !System.IO.Directory.Exists(dir))
                System.IO.Directory.CreateDirectory(dir);

            bool png = string.Equals(format, "png", StringComparison.OrdinalIgnoreCase);
            if (png) output.Save(outPath, ImageFormat.Png);
            else
            {
                ImageCodecInfo jpg = null;
                foreach (ImageCodecInfo c in ImageCodecInfo.GetImageEncoders())
                    if (c.FormatID == ImageFormat.Jpeg.Guid) { jpg = c; break; }
                using (EncoderParameters p = new EncoderParameters(1))
                {
                    p.Param[0] = new EncoderParameter(Encoder.Quality, (long)(quality <= 0 ? 75 : quality));
                    output.Save(outPath, jpg, p);
                }
            }

            long bytes = 0;
            try { bytes = new System.IO.FileInfo(outPath).Length; } catch { }

            if (scaled != null) scaled.Dispose();
            bmp.Dispose();

            // scale lets the caller map an image-space coordinate back to screen space.
            double scale = (double)ow / Math.Max(1, w);
            return "{\"path\":\"" + Js.Esc(outPath) + "\",\"srcX\":" + x + ",\"srcY\":" + y
                 + ",\"srcW\":" + w + ",\"srcH\":" + h
                 + ",\"width\":" + ow + ",\"height\":" + oh
                 + ",\"bytes\":" + bytes + ",\"scale\":" + scale.ToString("F6", CultureInfo.InvariantCulture) + "}";
        }
    }
}
