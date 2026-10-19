// chatcoder desktop-core: resident server entry point.
//
// PROTOCOL: one request per line, one response per line, both flat JSON objects.
//   request  {"op":"click","x":100,"y":200,"title":"Notepad","requireForeground":true}
//   response {"ok":true,"serverMs":3.12,"data":{...}}
// Flat request parsing (not a full JSON DOM) keeps the hot path allocation-free and lets
// the kernel run on a stock .NET Framework without extra dependencies.
//
// LIFECYCLE: the process is meant to outlive individual calls. It is started once by the
// Python backend and reused; the caller sends {"op":"shutdown"} to stop it. It exits on its
// own if the pipe closes and no client returns within a grace period, so a crashed backend
// cannot leave an orphan running forever.
//
// SAFETY: exactly one accepted command-line argument ("serve"). Anything else exits
// immediately. This process never starts another process or itself.

using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace DesktopCore
{
    internal static class Program
    {
        private const string DefaultPipeName = "chatcoder_desktop_core";
        private const double ServerVersion = 1.0;

        private static string _pipeName = DefaultPipeName;
        private static volatile bool _shutdown;

        [STAThread]
        private static int Main(string[] args)
        {
            // Guard: only "serve [pipeName]" starts the server. A missing or unexpected
            // argument exits at once rather than falling through to any default behaviour.
            if (args.Length < 1 || args[0] != "serve")
            {
                Console.Error.WriteLine("usage: desktop-core.exe serve [pipeName]");
                return 2;
            }
            if (args.Length > 1 && !string.IsNullOrEmpty(args[1]))
                _pipeName = args[1];

            // Declare Per-Monitor-V2 awareness before touching any UI API. Without this,
            // Windows virtualizes coordinates: GetSystemMetrics returns the scaled
            // resolution and clicks land in the wrong place on a 150% display.
            try { Native.SetProcessDpiAwarenessContext(Native.PMv2); }
            catch { /* pre-1703 Windows: continue at the inherited awareness level */ }

            Console.OutputEncoding = new UTF8Encoding(false);

            // The pipe server MUST exist before we announce readiness. Previously we printed
            // "ready" first and created the pipe afterwards, so a fast client could try to
            // connect in the gap and fail with WinError 2 (file not found).
            NamedPipeServerStream first;
            try
            {
                first = new NamedPipeServerStream(_pipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.None);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("cannot create pipe: " + ex.Message);
                return 3;
            }

            Console.WriteLine("{\"ready\":true,\"version\":" + ServerVersion.ToString("F1", CultureInfo.InvariantCulture)
                + ",\"pid\":" + Process.GetCurrentProcess().Id
                + ",\"pipe\":\"" + Js.Esc(_pipeName) + "\""
                + ",\"screen\":[" + Native.GetSystemMetrics(Native.SM_CXSCREEN) + ","
                + Native.GetSystemMetrics(Native.SM_CYSCREEN) + "],"
                + "\"virtual\":[" + Native.GetSystemMetrics(Native.SM_XVIRTUALSCREEN) + ","
                + Native.GetSystemMetrics(Native.SM_YVIRTUALSCREEN) + ","
                + Native.GetSystemMetrics(Native.SM_CXVIRTUALSCREEN) + ","
                + Native.GetSystemMetrics(Native.SM_CYVIRTUALSCREEN) + "]}");
            Console.Out.Flush();

            IdleWatchdog.Start();

            bool reuse = true;
            while (!_shutdown)
            {
                try { ServeOnce(first, reuse); }
                catch (Exception ex)
                {
                    // Never let one bad request kill the server; report and keep serving.
                    Console.Error.WriteLine("serve error: " + ex.GetType().Name + " | " + ex.Message);
                    Console.Error.Flush();
                }
                first = null;   // only the first iteration uses the pre-created instance
                reuse = false;
            }

            Console.WriteLine("{\"bye\":true}");
            return 0;
        }

        private static void ServeOnce(NamedPipeServerStream prepared, bool usePrepared)
        {
            NamedPipeServerStream server = usePrepared && prepared != null
                ? prepared
                : new NamedPipeServerStream(_pipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.None);

            using (server)
            {
                // Wait for a client with an explicit timeout loop.
                //
                // Two earlier approaches were wrong and have been replaced:
                //   * polling BeginWaitForConnection inside the loop started a SECOND wait
                //     while the first was pending, which threw and tore the pipe down;
                //   * polling AsyncWaitHandle.WaitOne was unreliable because a synchronously
                //     completed APM operation does not always signal its wait handle, so a
                //     successful connection could be mistaken for a timeout.
                // WaitForConnectionAsync plus Task.Wait(timeout) avoids both.
                System.Threading.Tasks.Task wait = server.WaitForConnectionAsync();
                while (!wait.Wait(250))
                {
                    if (_shutdown) return;
                    IdleWatchdog.MarkIdleStart();
                }

                IdleWatchdog.MarkBusy();

                using (StreamReader reader = new StreamReader(server, new UTF8Encoding(false), false, 65536, true))
                using (StreamWriter writer = new StreamWriter(server, new UTF8Encoding(false), 65536, true))
                {
                    writer.AutoFlush = true;
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        if (line.Trim().Length == 0) continue;

                        Stopwatch sw = Stopwatch.StartNew();
                        string data = Dispatch(line);
                        sw.Stop();

                        writer.WriteLine("{\"ok\":true,\"serverMs\":"
                            + sw.Elapsed.TotalMilliseconds.ToString("F3", CultureInfo.InvariantCulture)
                            + ",\"data\":" + data + "}");

                        if (_shutdown) return;
                    }
                }
            }
        }

        private static string Dispatch(string line)
        {
            string op = Js.GetStr(line, "op", "");
            try
            {
                switch (op)
                {
                    case "ping":
                        return "{\"pong\":true}";
                    case "info":
                        return Info();
                    case "shutdown":
                        _shutdown = true;
                        return "{\"stopping\":true}";

                    // ---- sensing ----
                    case "windows":
                        return Windows(line);
                    case "snapshot":
                        return Snapshot(line);
                    case "hit":
                        return Hit(line);
                    case "shot":
                        return Shot(line);
                    case "zoom":
                        return ZoomOp(line);
                    case "waitstable":
                        return WaitStableOp(line);
                    case "settle":
                        return SettleOp(line);
                    case "frame":
                        return FrameOp(line);
                    case "proccmd":
                        return ProcCmd(line);
                    case "listenports":
                        return ListenPorts(line);

                    // ---- applications ----
                    case "findapp":
                        return FindApp(line);
                    case "startapp":
                        return StartApp(line);

                    // ---- execution ----
                    case "focus":
                        return FocusOp(line);
                    case "click":
                        return ClickOp(line);
                    case "type":
                        return TypeOp(line);
                    case "keys":
                        return KeysOp(line);
                    case "scroll":
                        return ScrollOp(line);
                    case "drag":
                        return DragOp(line);
                    case "move":
                        return MoveOp(line);
                    case "invoke":
                        return InvokeOp(line);
                    case "settext":
                        return SetTextOp(line);

                    default:
                        return "{\"error\":\"unknown op: " + Js.Esc(op) + "\"}";
                }
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Js.Esc(ex.GetType().Name + ": " + ex.Message) + "\"}";
            }
        }

        private static string Info()
        {
            int vx = Native.GetSystemMetrics(Native.SM_XVIRTUALSCREEN);
            int vy = Native.GetSystemMetrics(Native.SM_YVIRTUALSCREEN);
            int vw = Native.GetSystemMetrics(Native.SM_CXVIRTUALSCREEN);
            int vh = Native.GetSystemMetrics(Native.SM_CYVIRTUALSCREEN);
            return "{\"version\":" + ServerVersion.ToString("F1", CultureInfo.InvariantCulture)
                 + ",\"pid\":" + Process.GetCurrentProcess().Id
                 + ",\"screen\":[" + Native.GetSystemMetrics(Native.SM_CXSCREEN) + ","
                 + Native.GetSystemMetrics(Native.SM_CYSCREEN) + "]"
                 + ",\"virtualScreen\":{\"x\":" + vx + ",\"y\":" + vy + ",\"w\":" + vw + ",\"h\":" + vh + "}"
                 + ",\"uptime\":"
                 + ((int)(DateTime.UtcNow - Process.GetCurrentProcess().StartTime.ToUniversalTime()).TotalSeconds)
                 + "}";
        }

        // ── sensing ops ──────────────────────────────────────────────────────

        private static string Windows(string line)
        {
            bool visibleOnly = Js.GetBool(line, "visibleOnly", true);
            int limit = Js.GetInt(line, "limit", 80);
            string filter = Js.GetStr(line, "filter", "");

            System.Collections.Generic.List<WinInfo> list = Sensing.ListWindows(visibleOnly, limit * 3);
            StringBuilder sb = new StringBuilder(8192);
            int n = 0;
            foreach (WinInfo w in list)
            {
                if (filter.Length > 0
                    && w.Title.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0
                    && w.ProcessName.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0)
                    continue;
                if (n >= limit) break;

                if (n > 0) sb.Append(',');
                sb.Append("{\"h\":").Append(w.Handle)
                  .Append(",\"title\":\"").Append(Js.Esc(w.Title))
                  .Append("\",\"cls\":\"").Append(Js.Esc(w.ClassName))
                  .Append("\",\"pid\":").Append(w.Pid)
                  .Append(",\"proc\":\"").Append(Js.Esc(w.ProcessName))
                  .Append("\",\"rect\":[").Append(w.Left).Append(',').Append(w.Top)
                  .Append(',').Append(w.Width).Append(',').Append(w.Height).Append(']')
                  .Append(",\"min\":").Append(w.Minimized ? "true" : "false")
                  .Append(",\"fg\":").Append(w.Foreground ? "true" : "false")
                  .Append(",\"x\":").Append(w.Left + w.Width / 2)
                  .Append(",\"y\":").Append(w.Top + w.Height / 2)
                  .Append('}');
                n++;
            }
            IntPtr fg = Sensing.Foreground;
            StringBuilder ft = new StringBuilder(512);
            Native.GetWindowTextW(fg, ft, 512);
            return "{\"count\":" + n + ",\"foregroundTitle\":\"" + Js.Esc(ft.ToString())
                 + "\",\"items\":[" + sb + "]}";
        }

        /// <summary>
        /// 读取指定进程的命令行（plan-340-1705）。
        ///
        /// 用途：Chromium 系应用（Chrome/Edge/Electron/WebView2）如果带
        /// `--remote-debugging-port=N` 启动，就能走 CDP 快速取到完整界面元素（实测 8 ms、
        /// 信息完整）；而 UIA 在同类窗口上既慢（171 ms）又只能拿到窗口边框按钮。
        /// 要用 CDP 就必须先知道端口，命令行是最直接的来源——它按 pid 与窗口精确对应。
        ///
        /// 两条路径：先直读 PEB（约 1 ms），失败再用 WMI 兜底（约 195 ms）。
        /// 这个快慢差距正是感知链路的瓶颈：实测 615 ms 的总耗时里 WMI 独占 195 ms。
        /// </summary>
        private static string ProcCmd(string line)
        {
            int pid = Js.GetInt(line, "pid", 0);
            if (pid <= 0) return "{\"error\":\"缺少 pid\"}";

            string cmd = ReadCmdlineFast((uint)pid);
            string via = "peb";
            if (cmd.Length == 0)
            {
                cmd = ReadCmdlineWmi(pid);
                via = "wmi";
            }
            return "{\"pid\":" + pid + ",\"cmdline\":\"" + Js.Esc(cmd)
                 + "\",\"via\":\"" + via + "\"}";
        }

        /// <summary>
        /// 直读目标进程 PEB 里的命令行（x64 快路径）。
        ///
        /// 32 位目标进程与 32 位内核进程都不走这里：WOW64 的 PEB 布局与偏移都不同，
        /// 硬编码 32 位偏移的风险远高于收益（Chromium 系应用基本都是 64 位），
        /// 这两类情况一律交给 WMI 兜底。
        /// </summary>
        private static string ReadCmdlineFast(uint pid)
        {
            if (!Environment.Is64BitProcess) return "";

            IntPtr h = Native.OpenProcess(
                Native.PROCESS_QUERY_LIMITED_INFORMATION | Native.PROCESS_VM_READ, false, pid);
            if (h == IntPtr.Zero) return "";
            try
            {
                bool wow64 = false;
                if (Native.IsWow64Process(h, out wow64) && wow64) return "";

                Native.PROCESS_BASIC_INFORMATION pbi = new Native.PROCESS_BASIC_INFORMATION();
                int ret;
                int sz = Marshal.SizeOf(typeof(Native.PROCESS_BASIC_INFORMATION));
                if (Native.NtQueryInformationProcess(h, 0, ref pbi, sz, out ret) != 0) return "";
                if (pbi.PebBaseAddress == IntPtr.Zero) return "";

                // PEB.ProcessParameters 在 x64 的偏移是 0x20
                IntPtr ppParams = ReadPtr(h, Add(pbi.PebBaseAddress, 0x20));
                if (ppParams == IntPtr.Zero) return "";

                // RTL_USER_PROCESS_PARAMETERS.CommandLine 在 x64 的偏移是 0x70；
                // UNICODE_STRING = {USHORT Length; USHORT MaxLength; PWSTR Buffer}，
                // Buffer 因指针对齐落在 +8 而非 +4。
                IntPtr us = Add(ppParams, 0x70);
                ushort len = ReadU16(h, us);
                if (len == 0 || len > 32760) return "";
                IntPtr buf = ReadPtr(h, Add(us, 8));
                if (buf == IntPtr.Zero) return "";

                byte[] bytes = ReadBytes(h, buf, len);
                if (bytes == null || bytes.Length == 0) return "";
                return Encoding.Unicode.GetString(bytes).TrimEnd('\0');
            }
            catch
            {
                return "";
            }
            finally
            {
                Native.CloseHandle(h);
            }
        }

        /// <summary>WMI 兜底：能覆盖 32 位目标进程，但单次约 195 ms。</summary>
        private static string ReadCmdlineWmi(int pid)
        {
            try
            {
                using (System.Management.ManagementObjectSearcher searcher =
                    new System.Management.ManagementObjectSearcher(
                        "SELECT CommandLine FROM Win32_Process WHERE ProcessId = " + pid))
                {
                    foreach (System.Management.ManagementObject mo in searcher.Get())
                    {
                        object v = mo["CommandLine"];
                        return v == null ? "" : v.ToString();
                    }
                }
            }
            catch { }
            return "";
        }

        private static IntPtr Add(IntPtr p, int off)
        {
            return new IntPtr(p.ToInt64() + off);
        }

        private static byte[] ReadBytes(IntPtr h, IntPtr addr, int size)
        {
            if (size <= 0 || size > (1 << 20)) return null;
            byte[] buf = new byte[size];
            IntPtr read;
            if (!Native.ReadProcessMemory(h, addr, buf, size, out read)) return null;
            if (read.ToInt64() < size) return null;
            return buf;
        }

        private static IntPtr ReadPtr(IntPtr h, IntPtr addr)
        {
            byte[] b = ReadBytes(h, addr, IntPtr.Size);
            if (b == null || b.Length < IntPtr.Size) return IntPtr.Zero;
            if (IntPtr.Size == 8) return new IntPtr(BitConverter.ToInt64(b, 0));
            return new IntPtr(BitConverter.ToInt32(b, 0));
        }

        private static ushort ReadU16(IntPtr h, IntPtr addr)
        {
            byte[] b = ReadBytes(h, addr, 2);
            if (b == null || b.Length < 2) return 0;
            return BitConverter.ToUInt16(b, 0);
        }

        /// <summary>
        /// 列出某进程正在监听的 TCP 端口（plan-340-1705）。
        ///
        /// 用途：有些应用通过环境变量注入调试参数（WebView2 的
        /// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS），命令行里未必看得到，但它一定在监听调试端口。
        /// 按 pid 精确枚举监听端口，比"盲扫常见端口"既快又准——实测盲扫要 511 ms，
        /// 且可能误配到别的应用恰好开着的调试端口。
        /// </summary>
        private static string ListenPorts(string line)
        {
            int pid = Js.GetInt(line, "pid", 0);
            if (pid <= 0) return "{\"error\":\"缺少 pid\"}";

            System.Collections.Generic.List<int> ports = new System.Collections.Generic.List<int>();
            int size = 0;
            Native.GetExtendedTcpTable(IntPtr.Zero, ref size, false, 2, 5, 0);
            if (size > 0)
            {
                IntPtr buf = Marshal.AllocHGlobal(size);
                try
                {
                    if (Native.GetExtendedTcpTable(buf, ref size, false, 2, 5, 0) == 0)
                    {
                        int count = Marshal.ReadInt32(buf);
                        long row = buf.ToInt64() + 4;
                        for (int i = 0; i < count; i++)
                        {
                            // MIB_TCPROW_OWNER_PID：6 个 DWORD（state, localAddr, localPort,
                            // remoteAddr, remotePort, ownerPid），行宽 24 字节
                            int state = Marshal.ReadInt32(new IntPtr(row));
                            int localPort = Marshal.ReadInt32(new IntPtr(row + 8));
                            int owner = Marshal.ReadInt32(new IntPtr(row + 20));
                            if (state == 2 && owner == pid)   // 2 = LISTEN
                            {
                                // dwLocalPort 为网络字节序，落在 DWORD 的低 16 位
                                int port = ((localPort & 0xFF) << 8) | ((localPort >> 8) & 0xFF);
                                if (!ports.Contains(port)) ports.Add(port);
                            }
                            row += 24;
                        }
                    }
                }
                catch { }
                finally { Marshal.FreeHGlobal(buf); }
            }

            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < ports.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(ports[i]);
            }
            return "{\"pid\":" + pid + ",\"ports\":[" + sb + "]}";
        }

        private static string Snapshot(string line)
        {
            long handle = Js.GetLong(line, "handle", 0);
            IntPtr h = Sensing.Resolve(handle, Js.GetStr(line, "title", ""), Js.GetStr(line, "proc", ""));
            if (h == IntPtr.Zero) return "{\"error\":\"未找到目标窗口\"}";
            int budget = Js.GetInt(line, "budget", 400);
            bool interactiveOnly = Js.GetBool(line, "interactiveOnly", true);
            int depth = Js.GetInt(line, "depth", 0);
            int timeBudgetMs = Js.GetInt(line, "timeBudgetMs", 0);
            return Sensing.Snapshot(h, budget, interactiveOnly, depth, timeBudgetMs);
        }

        private static string Hit(string line)
        {
            int x = Js.GetInt(line, "x", 0), y = Js.GetInt(line, "y", 0);
            bool chain = Js.GetBool(line, "chain", true);
            return Sensing.HitAt(x, y, chain);
        }

        private static string Shot(string line)
        {
            return Sensing.Capture(
                Js.GetStr(line, "path", ""),
                Js.GetInt(line, "x", 0), Js.GetInt(line, "y", 0),
                Js.GetInt(line, "w", 0), Js.GetInt(line, "h", 0),
                Js.GetInt(line, "maxDim", 1600),
                Js.GetStr(line, "format", "jpg"),
                Js.GetInt(line, "quality", 75),
                Js.GetBool(line, "fit", false));
        }

        /// <summary>
        /// 围绕给定屏幕坐标放大截取一小块（plan-365-1757 A5）。
        ///
        /// 用途：列表里的小目标（复选框、缩略图、16px 级图标）在全屏缩放图上看不清，
        /// 业界（Anthropic 官方建议、通义 InnerZoom）验证「先粗定位、再放大复核」能显著提准。
        /// 裁剪范围自动夹在虚拟桌面内，避免越界取到黑边。
        /// </summary>
        private static string ZoomOp(string line)
        {
            int x = Js.GetInt(line, "x", 0), y = Js.GetInt(line, "y", 0);
            int radius = Js.GetInt(line, "radius", 160);
            if (radius < 20) radius = 20;
            if (radius > 1200) radius = 1200;

            int vx = Native.GetSystemMetrics(Native.SM_XVIRTUALSCREEN);
            int vy = Native.GetSystemMetrics(Native.SM_YVIRTUALSCREEN);
            int vw = Native.GetSystemMetrics(Native.SM_CXVIRTUALSCREEN);
            int vh = Native.GetSystemMetrics(Native.SM_CYVIRTUALSCREEN);
            if (vw <= 0) vw = Native.GetSystemMetrics(Native.SM_CXSCREEN);
            if (vh <= 0) vh = Native.GetSystemMetrics(Native.SM_CYSCREEN);

            int left = Math.Max(vx, x - radius);
            int top = Math.Max(vy, y - radius);
            int right = Math.Min(vx + vw, x + radius);
            int bottom = Math.Min(vy + vh, y + radius);
            int w = Math.Max(1, right - left);
            int h = Math.Max(1, bottom - top);

            return Sensing.Capture(
                Js.GetStr(line, "path", ""), left, top, w, h,
                Js.GetInt(line, "maxDim", 1200),
                Js.GetStr(line, "format", "jpg"),
                Js.GetInt(line, "quality", 75));
        }

        /// <summary>等待界面稳定（旧 op，保留兼容）：口径已改为容差比较，见 Sensing.Settle。</summary>
        private static string WaitStableOp(string line)
        {
            return Sensing.WaitStable(
                Js.GetInt(line, "x", 0), Js.GetInt(line, "y", 0),
                Js.GetInt(line, "w", 0), Js.GetInt(line, "h", 0),
                Js.GetInt(line, "timeout", 3000),
                Js.GetInt(line, "settle", 60));
        }

        /// <summary>
        /// 独立的稳定等待（plan-20261019-desktop-precision）：截图前 wait_stable、批量动作收尾时用，
        /// 只判稳定、不比较动作前后。区域缺省为整个虚拟桌面。
        /// </summary>
        private static string SettleOp(string line)
        {
            return Sensing.Settle(
                Js.GetInt(line, "x", 0), Js.GetInt(line, "y", 0),
                Js.GetInt(line, "w", 0), Js.GetInt(line, "h", 0),
                null, false, 0, 0,
                Js.GetInt(line, "minMs", 0), Js.GetInt(line, "timeout", 1500),
                Js.GetInt(line, "interval", 60), 0);
        }

        // 请求里没有动作点（lx/ly）时的占位值
        private const int NoPoint = int.MinValue;

        /// <summary>
        /// 动作前按需采样（请求带 settle=true 时，plan-20261019-desktop-precision）。
        /// 区域 sx/sy/sw/sh 为调用方的当前画面范围（窗口或整屏），缺省为整个虚拟桌面。
        /// </summary>
        private static Sensing.Sample BeginSettle(string line, int px, int py)
        {
            if (!Js.GetBool(line, "settle", false)) return null;
            return Sensing.TakeSample(
                Js.GetInt(line, "sx", 0), Js.GetInt(line, "sy", 0),
                Js.GetInt(line, "sw", 0), Js.GetInt(line, "sh", 0),
                px != NoPoint, px, py);
        }

        /// <summary>动作后等待稳定并判断有无可见变化，返回拼到响应里的 ,"settle":{...} 片段。</summary>
        private static string EndSettle(string line, Sensing.Sample before, int px, int py)
        {
            if (before == null) return "";
            return ",\"settle\":" + Sensing.Settle(
                Js.GetInt(line, "sx", 0), Js.GetInt(line, "sy", 0),
                Js.GetInt(line, "sw", 0), Js.GetInt(line, "sh", 0),
                before, px != NoPoint, px, py,
                Js.GetInt(line, "settleMinMs", 150), Js.GetInt(line, "settleMs", 1500),
                Js.GetInt(line, "settleIntervalMs", 60), Js.GetInt(line, "noChangeMs", 900));
        }

        /// <summary>
        /// 窗口可见边界（plan-365-1757 A6）：同时给出 DWM 扩展帧与 GetWindowRect 两种矩形，
        /// 调用方据此把 CDP 的视口坐标换算到屏幕，消除不可见边框带来的系统偏差。
        /// </summary>
        private static string FrameOp(string line)
        {
            IntPtr h = Sensing.Resolve(Js.GetLong(line, "handle", 0),
                                       Js.GetStr(line, "title", ""), Js.GetStr(line, "proc", ""));
            if (h == IntPtr.Zero) return "{\"error\":\"未找到目标窗口\"}";

            Native.RECT fr = Sensing.VisibleFrame(h);
            Native.RECT wr;
            Native.GetWindowRect(h, out wr);
            return "{\"handle\":" + h.ToInt64()
                 + ",\"frame\":[" + fr.Left + "," + fr.Top + ","
                 + (fr.Right - fr.Left) + "," + (fr.Bottom - fr.Top) + "]"
                 + ",\"window\":[" + wr.Left + "," + wr.Top + ","
                 + (wr.Right - wr.Left) + "," + (wr.Bottom - wr.Top) + "]}";
        }

        // ── applications ─────────────────────────────────────────────────────

        private static string FindApp(string line)
        {
            System.Collections.Generic.List<AppHit> hits =
                Apps.Find(Js.GetStr(line, "keyword", ""), Js.GetInt(line, "limit", 10));
            StringBuilder sb = new StringBuilder(2048);
            int n = 0;
            foreach (AppHit h in hits)
            {
                if (n > 0) sb.Append(',');
                sb.Append("{\"name\":\"").Append(Js.Esc(h.Name))
                  .Append("\",\"exe\":\"").Append(Js.Esc(h.Exe))
                  .Append("\",\"score\":").Append(h.Score).Append('}');
                n++;
            }
            return "{\"count\":" + n + ",\"items\":[" + sb + "]}";
        }

        private static string StartApp(string line)
        {
            return Apps.Start(Js.GetStr(line, "exe", ""), Js.GetStr(line, "keyword", ""),
                              Js.GetInt(line, "timeout", 8000));
        }

        // ── execution ops ────────────────────────────────────────────────────

        private static string FocusOp(string line)
        {
            IntPtr h = Sensing.Resolve(Js.GetLong(line, "handle", 0),
                                       Js.GetStr(line, "title", ""), Js.GetStr(line, "proc", ""));
            if (h == IntPtr.Zero) return "{\"error\":\"未找到目标窗口\"}";
            bool ok = Sensing.Focus(h);
            return "{\"focused\":" + (ok ? "true" : "false")
                 + ",\"handle\":" + h.ToInt64()
                 + ",\"foreground\":" + (Native.GetForegroundWindow() == h ? "true" : "false") + "}";
        }

        /// <summary>
        /// focusFirst=true 表示调用方显式授权本次操作主动激活目标窗口：先激活，再做前台校验，
        /// 使「激活 + 操作」一步完成，省掉模型单独跑一次 focus 的往返；
        /// 激活失败时前台校验照样拦截，安全护栏没有被绕过。
        /// </summary>
        private static bool MaybeFocusFirst(string line)
        {
            if (!Js.GetBool(line, "focusFirst", false)) return false;
            Input.Target t0 = Input.ResolveTarget(
                Js.GetLong(line, "handle", 0), Js.GetStr(line, "title", ""),
                Js.GetStr(line, "proc", ""), false);
            if (t0.Handle == IntPtr.Zero) return false;
            return Sensing.Focus(t0.Handle);
        }

        private static string ClickOp(string line)
        {
            int x = Js.GetInt(line, "x", 0), y = Js.GetInt(line, "y", 0);
            bool requireFg = Js.GetBool(line, "requireForeground", true);

            bool focusedNow = MaybeFocusFirst(line);

            Input.Target t = Input.ResolveTarget(
                Js.GetLong(line, "handle", 0), Js.GetStr(line, "title", ""),
                Js.GetStr(line, "proc", ""), requireFg);
            if (t.Error.Length > 0) return "{\"error\":\"" + Js.Esc(t.Error) + "\"}";

            // 先移到目标、等悬停态生效，再采样「动作前」画面（plan-20261019-desktop-precision），
            // 最后按下：悬停效果不会被算成点击引起的变化
            Input.MoveTo(x, y);
            System.Threading.Thread.Sleep(30);
            Sensing.Sample before = BeginSettle(line, x, y);
            bool ok = Input.Press(Js.GetStr(line, "button", "left"), Js.GetInt(line, "count", 1));

            // Optional "click then type then press Enter" in one call: this is the single
            // biggest step-count saver, because each round trip to the model is where the
            // real latency lives.
            StringBuilder extra = new StringBuilder();
            string thenText = Js.GetStr(line, "text", "");
            if (thenText.Length > 0)
            {
                System.Threading.Thread.Sleep(120);
                if (Js.GetBool(line, "clearFirst", false))
                {
                    // 先全选再输入：一步完成「清空 + 覆盖」（plan-365-1774）。
                    Input.Keys("ctrl+a");
                    System.Threading.Thread.Sleep(60);
                }
                int n = Input.TypeText(thenText, Js.GetInt(line, "charDelay", 0));
                extra.Append(",\"typed\":").Append(n);
            }
            string thenKeys = Js.GetStr(line, "keys", "");
            if (thenKeys.Length > 0)
            {
                System.Threading.Thread.Sleep(80);
                string err = Input.Keys(thenKeys);
                if (err.Length > 0) extra.Append(",\"keysError\":\"").Append(Js.Esc(err)).Append('"');
            }
            extra.Append(EndSettle(line, before, x, y));

            return "{\"clicked\":" + (ok ? "true" : "false")
                 + ",\"at\":[" + x + "," + y + "]"
                 + ",\"target\":\"" + Js.Esc(t.Title) + "\""
                 + ",\"targetProc\":\"" + Js.Esc(t.ProcessName) + "\""
                 + ",\"foreground\":" + (t.IsForeground ? "true" : "false")
                 + ",\"focusedNow\":" + (focusedNow ? "true" : "false")
                 + extra + "}";
        }

        private static string TypeOp(string line)
        {
            bool requireFg = Js.GetBool(line, "requireForeground", true);

            bool focusedNow = MaybeFocusFirst(line);

            Input.Target t = Input.ResolveTarget(
                Js.GetLong(line, "handle", 0), Js.GetStr(line, "title", ""),
                Js.GetStr(line, "proc", ""), requireFg);
            if (t.Error.Length > 0) return "{\"error\":\"" + Js.Esc(t.Error) + "\"}";

            // 输入没有天然的动作点：调用方可用 lx/ly 传最近一次点击的位置（通常就是输入框）
            int lx = Js.GetInt(line, "lx", NoPoint), ly = Js.GetInt(line, "ly", NoPoint);
            Sensing.Sample before = BeginSettle(line, lx, lx == NoPoint ? NoPoint : ly);

            string text = Js.GetStr(line, "text", "");
            if (Js.GetBool(line, "clearFirst", false))
            {
                // Select-all then overwrite: works across most editors without knowing the
                // control type, and avoids a separate model round trip.
                Input.Keys("ctrl+a");
                System.Threading.Thread.Sleep(60);
            }
            int n = Input.TypeText(text, Js.GetInt(line, "charDelay", 0));

            // 输入完顺手按键（如 ENTER 提交）：一步完成「输入 + 回车」（plan-365-1774）。
            string afterKeys = Js.GetStr(line, "keys", "");
            string keysErr = "";
            if (afterKeys.Length > 0)
            {
                System.Threading.Thread.Sleep(80);
                keysErr = Input.Keys(afterKeys);
            }

            return "{\"typed\":" + n + ",\"target\":\"" + Js.Esc(t.Title) + "\""
                 + ",\"focusedNow\":" + (focusedNow ? "true" : "false")
                 + (keysErr.Length > 0 ? ",\"keysError\":\"" + Js.Esc(keysErr) + "\"" : "")
                 + EndSettle(line, before, lx, lx == NoPoint ? NoPoint : ly)
                 + "}";
        }

        private static string KeysOp(string line)
        {
            bool requireFg = Js.GetBool(line, "requireForeground", true);

            bool focusedNow = MaybeFocusFirst(line);

            Input.Target t = Input.ResolveTarget(
                Js.GetLong(line, "handle", 0), Js.GetStr(line, "title", ""),
                Js.GetStr(line, "proc", ""), requireFg);
            if (t.Error.Length > 0) return "{\"error\":\"" + Js.Esc(t.Error) + "\"}";

            int lx = Js.GetInt(line, "lx", NoPoint), ly = Js.GetInt(line, "ly", NoPoint);
            if (lx == NoPoint) ly = NoPoint;
            Sensing.Sample before = BeginSettle(line, lx, ly);
            string err = Input.Keys(Js.GetStr(line, "keys", ""));
            if (err.Length > 0) return "{\"error\":\"" + Js.Esc(err) + "\"}";
            return "{\"sent\":true,\"target\":\"" + Js.Esc(t.Title) + "\""
                 + ",\"focusedNow\":" + (focusedNow ? "true" : "false")
                 + EndSettle(line, before, lx, ly) + "}";
        }

        private static string ScrollOp(string line)
        {
            int x = Js.GetInt(line, "x", -1), y = Js.GetInt(line, "y", -1);
            if (x < 0 || y < 0)
            {
                // Default to the centre of the screen so scroll works without a hit test.
                x = Native.GetSystemMetrics(Native.SM_CXSCREEN) / 2;
                y = Native.GetSystemMetrics(Native.SM_CYSCREEN) / 2;
            }
            int delta = Js.GetInt(line, "delta", 0);
            if (delta == 0)
            {
                int clicks = Js.GetInt(line, "clicks", 3);
                delta = clicks * 120;   // WHEEL_DELTA
            }
            Input.MoveTo(x, y);
            System.Threading.Thread.Sleep(20);
            Sensing.Sample before = BeginSettle(line, x, y);
            bool ok = Input.Wheel(delta, Js.GetBool(line, "horizontal", false));
            return "{\"scrolled\":" + (ok ? "true" : "false") + ",\"delta\":" + delta
                 + EndSettle(line, before, x, y) + "}";
        }

        private static string DragOp(string line)
        {
            int x2 = Js.GetInt(line, "x2", 0), y2 = Js.GetInt(line, "y2", 0);
            Sensing.Sample before = BeginSettle(line, x2, y2);
            bool ok = Input.Drag(Js.GetInt(line, "x1", 0), Js.GetInt(line, "y1", 0),
                                 x2, y2, Js.GetInt(line, "duration", 300));
            return "{\"dragged\":" + (ok ? "true" : "false") + EndSettle(line, before, x2, y2) + "}";
        }

        /// <summary>移动鼠标（悬停）：悬停才展开的菜单、提示需要它（plan-20261019-desktop-precision）。</summary>
        private static string MoveOp(string line)
        {
            int x = Js.GetInt(line, "x", 0), y = Js.GetInt(line, "y", 0);
            Sensing.Sample before = BeginSettle(line, x, y);
            bool ok = Input.MoveTo(x, y);
            return "{\"moved\":" + (ok ? "true" : "false") + ",\"at\":[" + x + "," + y + "]"
                 + EndSettle(line, before, x, y) + "}";
        }

        private static string InvokeOp(string line)
        {
            IntPtr h = Sensing.Resolve(Js.GetLong(line, "handle", 0),
                                       Js.GetStr(line, "title", ""), Js.GetStr(line, "proc", ""));
            if (h == IntPtr.Zero) return "{\"error\":\"未找到目标窗口\"}";
            string err = Input.InvokePattern(h, Js.GetStr(line, "automationId", ""),
                                                Js.GetStr(line, "name", ""));
            if (err.Length > 0) return "{\"error\":\"" + Js.Esc(err) + "\"}";
            return "{\"invoked\":true}";
        }

        private static string SetTextOp(string line)
        {
            IntPtr h = Sensing.Resolve(Js.GetLong(line, "handle", 0),
                                       Js.GetStr(line, "title", ""), Js.GetStr(line, "proc", ""));
            if (h == IntPtr.Zero) return "{\"error\":\"未找到目标窗口\"}";
            string err = Input.SetValuePattern(h, Js.GetStr(line, "automationId", ""),
                                                   Js.GetStr(line, "name", ""),
                                                   Js.GetStr(line, "text", ""));
            if (err.Length > 0) return "{\"error\":\"" + Js.Esc(err) + "\"}";
            return "{\"set\":true}";
        }
    }

    /// <summary>
    /// Exits the process when no client has connected for a while. Without this, a backend
    /// crash leaves the kernel running forever holding a pipe name.
    /// </summary>
    internal static class IdleWatchdog
    {
        private static volatile bool _busy;
        private static DateTime _idleSince = DateTime.UtcNow;
        private const int IdleExitSeconds = 600;   // 10 minutes

        public static void MarkBusy() { _busy = true; }
        public static void MarkIdleStart() { if (!_busy) _idleSince = DateTime.UtcNow; }

        public static void Start()
        {
            Thread t = new Thread(() =>
            {
                while (true)
                {
                    Thread.Sleep(30000);
                    if (!_busy && (DateTime.UtcNow - _idleSince).TotalSeconds > IdleExitSeconds)
                    {
                        Console.WriteLine("{\"bye\":\"idle\"}");
                        Console.Out.Flush();
                        Environment.Exit(0);
                    }
                }
            });
            t.IsBackground = true;
            t.Start();
        }
    }
}
