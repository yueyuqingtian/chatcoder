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
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
        // 前台锁定（SetForegroundWindow 被系统静默拒绝）的标准解法需要这三个 API
        [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool fAttach);
        [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
        [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] p, int cb);
        [DllImport("gdi32.dll")] public static extern bool BitBlt(IntPtr d, int xd, int yd, int w, int h, IntPtr s, int xs, int ys, int rop);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
        [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int w, int h);
        [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hdc, IntPtr o);
        [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr o);
        [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hdc);

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

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }

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
        /// </summary>
        public static void Normalize(int x, int y, out int nx, out int ny)
        {
            int vx = GetSystemMetrics(SM_XVIRTUALSCREEN), vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
            int vw = GetSystemMetrics(SM_CXVIRTUALSCREEN), vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            if (vw <= 0) vw = GetSystemMetrics(SM_CXSCREEN);
            if (vh <= 0) vh = GetSystemMetrics(SM_CYSCREEN);
            nx = (int)Math.Round((x - vx) * 65535.0 / Math.Max(1, vw - 1));
            ny = (int)Math.Round((y - vy) * 65535.0 / Math.Max(1, vh - 1));
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
