// chatcoder desktop-core: resident Windows desktop-control kernel.
//
// DESIGN NOTES (why this shape):
//   * One long-lived process, one named-pipe connection, one JSON line per request.
//     Measured on this machine: a live pipe round trip costs ~0.01 ms, while spawning a
//     fresh process costs 30 ms (C# exe) to 430 ms (PowerShell). Staying resident removes
//     the fixed per-call overhead entirely.
//   * Sensing is tiered by measured cost, cheapest first:
//       ElementFromPoint        ~2.5 ms   -> "what is under this pixel?"
//       CacheRequest + FindAll  ~53 ms    -> full element list with geometry
//       screenshot              ~110 ms   -> last resort for self-drawn UIs
//     CacheRequest matters: reading properties one by one costs one cross-process COM
//     call per property, which measured 1037 ms for 600 nodes versus 360 ms batched.
//   * Input goes through SendInput with KEYEVENTF_UNICODE so CJK text is delivered
//     literally, never via the clipboard.
//
// SAFETY CONTRACT:
//   * The process accepts exactly one argument ("serve"). Any other argument exits
//     immediately. This process never launches itself or any other process.
//   * Typing/clicking is refused unless the target window can be resolved AND proven to
//     be foreground (callers may opt out per-call, but the check is on by default).
//
// COMPATIBILITY: compiled with the .NET Framework csc.exe in C# 5 syntax, so the same
// source builds on a stock Windows box with no SDK installed.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;

namespace DesktopCore
{
    internal static class Native
    {
        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
        [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
        [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
        // 旧系统 / 清单未生效时的兜底：至少让坐标是物理像素（System-Aware 在单显示器上与 PMv2 等价）
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
        // 前台锁定（SetForegroundWindow 被系统静默拒绝）的标准解法需要这三个 API
        [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool fAttach);
        [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        // 光标读回：点击落点校验要读「系统实际把光标放在了哪」，而不是相信发出去的坐标
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
        // DPI 自检（plan-20261019-desktop-targeting）：进程 / 线程是否真的处于 Per-Monitor-V2，
        // 以及每块显示器的有效 DPI。感知级别不对时所有坐标都会被系统虚拟化，必须能被发现。
        [DllImport("user32.dll")] public static extern IntPtr GetThreadDpiAwarenessContext();
        [DllImport("user32.dll")] public static extern bool AreDpiAwarenessContextsEqual(IntPtr a, IntPtr b);
        [DllImport("user32.dll")] public static extern int GetAwarenessFromDpiAwarenessContext(IntPtr ctx);
        public delegate bool MonitorEnumProc(IntPtr hMon, IntPtr hdc, ref RECT rc, IntPtr data);
        [DllImport("user32.dll")] public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc cb, IntPtr data);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool GetMonitorInfoW(IntPtr hMon, ref MONITORINFOEX mi);
        [DllImport("shcore.dll")] public static extern int GetDpiForMonitor(IntPtr hMon, int type, out uint dx, out uint dy);
        [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
        [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] p, int cb);
        [DllImport("gdi32.dll")] public static extern bool BitBlt(IntPtr d, int xd, int yd, int w, int h, IntPtr s, int xs, int ys, int rop);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int w, int h);
        [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hdc, IntPtr o);
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr o);
        [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hdc);
        // 画面缩略采样（plan-20261019-desktop-precision）：StretchBlt + HALFTONE 一步完成「截取 + 区域平均缩小」，
        // 直接写进 DIB 内存读回像素，不经过 Bitmap 编码与文件，单次只需几毫秒，适合动作后高频轮询。
        [DllImport("gdi32.dll")]
        public static extern bool StretchBlt(IntPtr hdcDest, int xDest, int yDest, int wDest, int hDest,
                                             IntPtr hdcSrc, int xSrc, int ySrc, int wSrc, int hSrc, int rop);
        [DllImport("gdi32.dll")] public static extern int SetStretchBltMode(IntPtr hdc, int mode);
        // 直接读 DIB 内存前必须刷新 GDI 批处理队列，否则可能读到尚未绘制完成的像素
        [DllImport("gdi32.dll")] public static extern bool GdiFlush();
        [DllImport("gdi32.dll")] public static extern bool SetBrushOrgEx(IntPtr hdc, int x, int y, IntPtr prev);
        [DllImport("gdi32.dll")]
        public static extern IntPtr CreateDIBSection(IntPtr hdc, ref BITMAPINFO bmi, uint usage,
                                                     out IntPtr bits, IntPtr section, uint offset);
        public const int HALFTONE = 4;

        [StructLayout(LayoutKind.Sequential)]
        public struct BITMAPINFOHEADER
        {
            public uint biSize;
            public int biWidth, biHeight;
            public ushort biPlanes, biBitCount;
            public uint biCompression, biSizeImage;
            public int biXPelsPerMeter, biYPelsPerMeter;
            public uint biClrUsed, biClrImportant;
        }

        // 32 位 BI_RGB 不需要调色板；结构体仍需占一个 RGBQUAD 的位置
        [StructLayout(LayoutKind.Sequential)]
        public struct BITMAPINFO { public BITMAPINFOHEADER bmiHeader; public uint bmiColors; }

        // ── 进程查询（plan-340-1705）：进程名、命令行、按 pid 枚举监听端口 ──
        // 为什么不用 System.Diagnostics.Process：它为每个 pid 打开句柄并建立进程快照，
        // 在窗口较多时（几十个不同 pid）累计让 windows op 花到 ~115 ms；下面这些 API
        // 各只做一次轻量查询，同一 op 实测降到十几毫秒。
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool QueryFullProcessImageNameW(IntPtr h, uint flags, StringBuilder name, ref int size);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool IsWow64Process(IntPtr h, out bool wow64);
        [DllImport("ntdll.dll")]
        public static extern int NtQueryInformationProcess(IntPtr h, int cls, ref PROCESS_BASIC_INFORMATION info, int len, out int ret);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ReadProcessMemory(IntPtr h, IntPtr addr, byte[] buf, int size, out IntPtr read);
        // TCP_TABLE_OWNER_PID_LISTENER = 5，AF_INET = 2
        [DllImport("iphlpapi.dll", SetLastError = true)]
        public static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool order, int af, int tableClass, int reserved);

        // 窗口可见边界（plan-365-1757 A6）：GetWindowRect 含 DWM 不可见调整边框
        // （Windows 10/11 上左右各约 7–8 px），按它把 CDP 的视口坐标换算回屏幕会系统性偏移。
        // DWMWA_EXTENDED_FRAME_BOUNDS 返回的是用户实际看到的矩形。
        [DllImport("dwmapi.dll")]
        public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out RECT rect, int size);
        public const int DWMWA_EXTENDED_FRAME_BOUNDS = 9;

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct MONITORINFOEX
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct PROCESS_BASIC_INFORMATION
        {
            public IntPtr Reserved1;
            public IntPtr PebBaseAddress;
            public IntPtr Reserved2a;
            public IntPtr Reserved2b;
            public IntPtr UniqueProcessId;
            public IntPtr Reserved3;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X, Y; }

        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }

        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }

        [StructLayout(LayoutKind.Sequential)]
        public struct HARDWAREINPUT { public uint uMsg; public ushort wParamL, wParamH; }

        [StructLayout(LayoutKind.Explicit)]
        public struct INPUTUNION
        {
            [FieldOffset(0)] public MOUSEINPUT mi;
            [FieldOffset(0)] public KEYBDINPUT ki;
            [FieldOffset(0)] public HARDWAREINPUT hi;
        }

        // Sequential: on x64 the CLR pads 4 bytes after 'type', so the union lands on
        // offset 8 and sizeof(INPUT) == 40, which is what SendInput expects.
        [StructLayout(LayoutKind.Sequential)]
        public struct INPUT { public uint type; public INPUTUNION u; }

        public const int SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77;
        public const int SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79;
        public const int SM_CXSCREEN = 0, SM_CYSCREEN = 1;
        public const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
        public const uint MOUSEEVENTF_MOVE = 0x0001, MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
        public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010;
        public const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020, MOUSEEVENTF_MIDDLEUP = 0x0040;
        public const uint MOUSEEVENTF_WHEEL = 0x0800, MOUSEEVENTF_HWHEEL = 0x1000;
        public const uint MOUSEEVENTF_ABSOLUTE = 0x8000, MOUSEEVENTF_VIRTUALDESK = 0x4000;
        public const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;
        public const int SW_RESTORE = 9, SW_SHOW = 5;
        public const int SRCCOPY = 0x00CC0020;
        public static readonly IntPtr PMv2 = new IntPtr(-4);
        // 进程访问：只查询镜像路径与基本信息，不用 FULL 权限（对受保护进程也更友好）
        public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        public const uint PROCESS_VM_READ = 0x0010;

        /// <summary>
        /// Absolute mouse coordinates must be normalized into 0..65535 across the whole
        /// virtual desktop, otherwise multi-monitor and scaled setups land in the wrong place.
        ///
        /// 映射口径（plan-20261019-desktop-targeting）：系统把 0..65535 线性映射到虚拟桌面，
        /// 像素 = nx * 宽 / 65536。要命中像素 x，取 nx = ceil(x * 65536 / 宽)，往返不丢像素；
        /// 旧写法按 65535 / (宽 - 1) 取整，在 2560 级分辨率上会有 1 像素偏差。
        /// 无论公式多准，落点都由 Input.MoveTo 读回光标位置并用 SetCursorPos 校正，这里只负责「尽量一次到位」。
        /// </summary>
        public static void Normalize(int x, int y, out int nx, out int ny)
        {
            int vx, vy, vw, vh;
            VirtualScreen(out vx, out vy, out vw, out vh);
            nx = (int)Math.Ceiling((x - vx) * 65536.0 / vw);
            ny = (int)Math.Ceiling((y - vy) * 65536.0 / vh);
            if (nx < 0) nx = 0; else if (nx > 65535) nx = 65535;
            if (ny < 0) ny = 0; else if (ny > 65535) ny = 65535;
        }

        /// <summary>虚拟桌面矩形（物理像素）；取不到时退回主屏。</summary>
        public static void VirtualScreen(out int vx, out int vy, out int vw, out int vh)
        {
            vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
            vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
            vw = GetSystemMetrics(SM_CXVIRTUALSCREEN);
            vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            if (vw <= 0) vw = Math.Max(1, GetSystemMetrics(SM_CXSCREEN));
            if (vh <= 0) vh = Math.Max(1, GetSystemMetrics(SM_CYSCREEN));
        }

        /// <summary>
        /// 当前线程的 DPI 感知级别：PerMonitorV2 / PerMonitor / System / Unaware。
        /// 不是 PerMonitorV2 时，系统会把坐标按缩放比虚拟化——截图、窗口矩形、点击落点各自缩放不一致，
        /// 这正是「不同缩放比例下点击偏移」最典型的底层原因，所以必须能在运行时被检测出来。
        /// </summary>
        public static string DpiAwarenessName()
        {
            try
            {
                IntPtr ctx = GetThreadDpiAwarenessContext();
                if (AreDpiAwarenessContextsEqual(ctx, PMv2)) return "PerMonitorV2";
                int a = GetAwarenessFromDpiAwarenessContext(ctx);
                if (a == 2) return "PerMonitor";
                if (a == 1) return "System";
                return "Unaware";
            }
            catch { return "Unknown"; }
        }

        public static bool Send(INPUT[] arr)
        {
            if (arr == null || arr.Length == 0) return true;
            uint sent = SendInput((uint)arr.Length, arr, Marshal.SizeOf(typeof(INPUT)));
            return sent == arr.Length;
        }

        public static INPUT MouseInput(uint flags, int dx, int dy, uint data)
        {
            INPUT i = new INPUT();
            i.type = INPUT_MOUSE;
            i.u.mi.dwFlags = flags;
            i.u.mi.dx = dx; i.u.mi.dy = dy; i.u.mi.mouseData = data;
            return i;
        }

        public static INPUT KeyInput(ushort vk, ushort scan, uint flags)
        {
            INPUT i = new INPUT();
            i.type = INPUT_KEYBOARD;
            i.u.ki.wVk = vk; i.u.ki.wScan = scan; i.u.ki.dwFlags = flags;
            return i;
        }
    }

    /// <summary>Minimal sentinel readers for the flat request objects this kernel defines.</summary>
    internal static class Js
    {
        public static string GetStr(string s, string key, string dflt)
        {
            if (string.IsNullOrEmpty(s)) return dflt;
            string pat = "\"" + key + "\":";
            int i = s.IndexOf(pat, StringComparison.Ordinal);
            if (i < 0) return dflt;
            i += pat.Length;
            while (i < s.Length && s[i] == ' ') i++;
            if (i >= s.Length || s[i] != '"') return dflt;
            i++;
            StringBuilder sb = new StringBuilder();
            while (i < s.Length && s[i] != '"')
            {
                if (s[i] == '\\' && i + 1 < s.Length)
                {
                    i++;
                    char c = s[i];
                    if (c == 'n') sb.Append('\n');
                    else if (c == 'r') sb.Append('\r');
                    else if (c == 't') sb.Append('\t');
                    else sb.Append(c);
                }
                else sb.Append(s[i]);
                i++;
            }
            return sb.ToString();
        }

        public static long GetLong(string s, string key, long dflt)
        {
            if (string.IsNullOrEmpty(s)) return dflt;
            string pat = "\"" + key + "\":";
            int i = s.IndexOf(pat, StringComparison.Ordinal);
            if (i < 0) return dflt;
            i += pat.Length;
            while (i < s.Length && s[i] == ' ') i++;
            int j = i;
            while (j < s.Length && (char.IsDigit(s[j]) || s[j] == '-' || s[j] == '.')) j++;
            double d;
            if (double.TryParse(s.Substring(i, j - i), NumberStyles.Float, CultureInfo.InvariantCulture, out d))
                return (long)d;
            return dflt;
        }

        public static int GetInt(string s, string key, int dflt) { return (int)GetLong(s, key, dflt); }

        /// <summary>读取浮点数（plan-365-1757：复用截图时需要调用方传入的缩放比）。</summary>
        public static double GetDouble(string s, string key, double dflt)
        {
            if (string.IsNullOrEmpty(s)) return dflt;
            string pat = "\"" + key + "\":";
            int i = s.IndexOf(pat, StringComparison.Ordinal);
            if (i < 0) return dflt;
            i += pat.Length;
            while (i < s.Length && s[i] == ' ') i++;
            int j = i;
            while (j < s.Length && (char.IsDigit(s[j]) || s[j] == '-' || s[j] == '.')) j++;
            double d;
            if (double.TryParse(s.Substring(i, j - i), NumberStyles.Float,
                                CultureInfo.InvariantCulture, out d))
                return d;
            return dflt;
        }

        public static bool GetBool(string s, string key, bool dflt)
        {
            if (string.IsNullOrEmpty(s)) return dflt;
            string pat = "\"" + key + "\":";
            int i = s.IndexOf(pat, StringComparison.Ordinal);
            if (i < 0) return dflt;
            i += pat.Length;
            while (i < s.Length && s[i] == ' ') i++;
            if (i + 4 <= s.Length && s.Substring(i, Math.Min(4, s.Length - i)).StartsWith("true", StringComparison.Ordinal)) return true;
            if (i + 5 <= s.Length && s.Substring(i, Math.Min(5, s.Length - i)).StartsWith("false", StringComparison.Ordinal)) return false;
            return dflt;
        }

        /// <summary>Reads a JSON array of numbers, e.g. "[1,2,3]".</summary>
        public static List<int> GetIntList(string s, string key)
        {
            List<int> outp = new List<int>();
            if (string.IsNullOrEmpty(s)) return outp;
            string pat = "\"" + key + "\":";
            int i = s.IndexOf(pat, StringComparison.Ordinal);
            if (i < 0) return outp;
            i += pat.Length;
            while (i < s.Length && s[i] == ' ') i++;
            if (i >= s.Length || s[i] != '[') return outp;
            i++;
            StringBuilder cur = new StringBuilder();
            while (i < s.Length && s[i] != ']')
            {
                char c = s[i];
                if (char.IsDigit(c) || c == '-') cur.Append(c);
                else if (cur.Length > 0)
                {
                    int v; if (int.TryParse(cur.ToString(), out v)) outp.Add(v);
                    cur.Length = 0;
                }
                i++;
            }
            if (cur.Length > 0) { int v; if (int.TryParse(cur.ToString(), out v)) outp.Add(v); }
            return outp;
        }

        public static string Esc(string s)
        {
            if (s == null) return "";
            StringBuilder sb = new StringBuilder(s.Length + 16);
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c == '\n') sb.Append("\\n");
                else if (c == '\r') sb.Append("\\r");
                else if (c == '\t') sb.Append("\\t");
                else if (c < 0x20) sb.Append(' ');
                else sb.Append(c);
            }
            return sb.ToString();
        }
    }
}
