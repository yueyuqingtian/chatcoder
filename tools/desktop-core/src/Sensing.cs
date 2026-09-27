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
        public static string Snapshot(IntPtr root, int budget, bool interactiveOnly, int depthLimit,
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
                 + ",\"truncated\":" + (truncated ? "true" : "false") + "}";
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

        /// <summary>
        /// Screen capture. GDI BitBlt into a compatible bitmap measures ~33 ms for
        /// 2560x1600; JPEG encoding is separate because it dominates (~30 ms) and callers
        /// may want raw pixels.
        /// </summary>
        public static string Capture(string outPath, int x, int y, int w, int h, int maxDim,
                                      string format, int quality)
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
            if (maxDim > 0 && (w > maxDim || h > maxDim))
            {
                double sc = Math.Min((double)maxDim / w, (double)maxDim / h);
                ow = Math.Max(1, (int)Math.Round(w * sc));
                oh = Math.Max(1, (int)Math.Round(h * sc));
                scaled = new Bitmap(ow, oh, PixelFormat.Format24bppRgb);
                using (Graphics g2 = Graphics.FromImage(scaled))
                {
                    // Bilinear is measured-faster than bicubic and good enough for a model
                    // to read; the difference is not visible after downscaling.
                    g2.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBilinear;
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
