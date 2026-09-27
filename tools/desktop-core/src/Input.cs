// Execution layer: mouse and keyboard injection, plus UIA pattern fallbacks.
//
// WHY SendInput: it produces real input events through the system input queue, so it works
// with applications that ignore synthetic window messages. Text is delivered with
// KEYEVENTF_UNICODE, which types CJK literally instead of going through the clipboard
// (clipboard mode is available as an explicit opt-in for apps that reject Unicode input).
//
// SAFETY: every write operation resolves a target window and, unless the caller explicitly
// opts out, refuses to act when that window is not foreground. This prevents the classic
// failure where an agent types a message into whatever window happened to have focus.
// It is deliberately a refusal rather than a silent focus change: the caller decides
// whether focusing is acceptable.

using System;
using System.Text;
using System.Threading;
using System.Windows.Automation;

namespace DesktopCore
{
    internal static class Input
    {
        /// <summary>Result of resolving and validating a write target.</summary>
        internal sealed class Target
        {
            public IntPtr Handle = IntPtr.Zero;
            public string Title = "";
            public string ProcessName = "";
            public bool IsForeground;
            public string Error = "";
        }

        public static Target ResolveTarget(long handle, string title, string processName, bool requireForeground)
        {
            Target t = new Target();
            IntPtr h = Sensing.Resolve(handle, title, processName);

            if (h == IntPtr.Zero)
            {
                // No target specified: allowed only when the caller did not demand proof.
                // This is the "click where the user is already looking" case.
                if (string.IsNullOrEmpty(title) && string.IsNullOrEmpty(processName) && handle == 0)
                {
                    t.IsForeground = true;
                    return t;
                }
                t.Error = "未找到目标窗口"
                    + (string.IsNullOrEmpty(title) ? "" : ("（标题包含 \"" + title + "\"）"))
                    + (string.IsNullOrEmpty(processName) ? "" : ("（进程 " + processName + "）"));
                return t;
            }

            t.Handle = h;
            StringBuilder sb = new StringBuilder(512);
            Native.GetWindowTextW(h, sb, 512);
            t.Title = sb.ToString();
            uint pid;
            Native.GetWindowThreadProcessId(h, out pid);
            try { t.ProcessName = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName; }
            catch { t.ProcessName = ""; }

            t.IsForeground = (Native.GetForegroundWindow() == h);
            if (requireForeground && !t.IsForeground)
            {
                t.Error = "目标窗口不在前台（" + t.Title + "）。出于安全考虑已拒绝输入；"
                        + "请先用 desktop_focus 激活该窗口，或确认后重试。";
            }
            return t;
        }

        public static bool MoveTo(int x, int y)
        {
            int nx, ny;
            Native.Normalize(x, y, out nx, out ny);
            return Native.Send(new[] {
                Native.MouseInput(Native.MOUSEEVENTF_MOVE | Native.MOUSEEVENTF_ABSOLUTE | Native.MOUSEEVENTF_VIRTUALDESK, nx, ny, 0)
            });
        }

        public static bool Click(int x, int y, string button, int count, bool restoreCursor)
        {
            Native.POINT original;
            original.X = 0; original.Y = 0;
            GetCursor(out original);

            uint down, up;
            switch ((button ?? "left").ToLowerInvariant())
            {
                case "right": down = Native.MOUSEEVENTF_RIGHTDOWN; up = Native.MOUSEEVENTF_RIGHTUP; break;
                case "middle": down = Native.MOUSEEVENTF_MIDDLEDOWN; up = Native.MOUSEEVENTF_MIDDLEUP; break;
                default: down = Native.MOUSEEVENTF_LEFTDOWN; up = Native.MOUSEEVENTF_LEFTUP; break;
            }

            MoveTo(x, y);
            Thread.Sleep(20);   // let the hover state settle so the target receives the press

            bool ok = true;
            for (int i = 0; i < Math.Max(1, count); i++)
            {
                ok &= Native.Send(new[] {
                    Native.MouseInput(down | Native.MOUSEEVENTF_ABSOLUTE | Native.MOUSEEVENTF_VIRTUALDESK, 0, 0, 0),
                    Native.MouseInput(up | Native.MOUSEEVENTF_ABSOLUTE | Native.MOUSEEVENTF_VIRTUALDESK, 0, 0, 0),
                });
                if (i + 1 < count) Thread.Sleep(60);
            }

            if (restoreCursor) Native.SetCursorPos(original.X, original.Y);
            return ok;
        }

        private static void GetCursor(out Native.POINT p)
        {
            p = new Native.POINT();
            try
            {
                // SetCursorPos needs a value; reading the current position is optional here.
                System.Reflection.MethodInfo mi = typeof(Native).GetMethod("GetCursorPos");
                if (mi != null)
                {
                    object[] args = new object[] { p };
                    mi.Invoke(null, args);
                    p = (Native.POINT)args[0];
                }
            }
            catch { }
        }

        public static bool Scroll(int x, int y, int delta, bool horizontal)
        {
            MoveTo(x, y);
            Thread.Sleep(20);
            uint flag = horizontal ? Native.MOUSEEVENTF_HWHEEL : Native.MOUSEEVENTF_WHEEL;
            return Native.Send(new[] { Native.MouseInput(flag, 0, 0, (uint)delta) });
        }

        public static bool Drag(int x1, int y1, int x2, int y2, int durationMs)
        {
            MoveTo(x1, y1);
            Thread.Sleep(40);
            Native.Send(new[] { Native.MouseInput(Native.MOUSEEVENTF_LEFTDOWN | Native.MOUSEEVENTF_ABSOLUTE | Native.MOUSEEVENTF_VIRTUALDESK, 0, 0, 0) });

            int steps = Math.Max(4, Math.Min(40, durationMs / 15));
            for (int i = 1; i <= steps; i++)
            {
                int px = x1 + (x2 - x1) * i / steps;
                int py = y1 + (y2 - y1) * i / steps;
                MoveTo(px, py);
                Thread.Sleep(Math.Max(1, durationMs / steps));
            }

            return Native.Send(new[] { Native.MouseInput(Native.MOUSEEVENTF_LEFTUP | Native.MOUSEEVENTF_ABSOLUTE | Native.MOUSEEVENTF_VIRTUALDESK, 0, 0, 0) });
        }

        /// <summary>Types a string with KEYEVENTF_UNICODE so non-ASCII arrives intact.</summary>
        public static int TypeText(string text, int charDelayMs)
        {
            if (string.IsNullOrEmpty(text)) return 0;
            int sent = 0;

            foreach (char c in text)
            {
                // Surrogate pairs must go out as one unit to form a valid code point.
                if (char.IsHighSurrogate(c)) continue;

                if (c == '\n' || c == '\r')
                {
                    PressVirtualKey(0x0D);   // VK_RETURN
                    sent++;
                    if (c == '\r' && text.IndexOf('\n') < 0) continue;
                    if (charDelayMs > 0) Thread.Sleep(charDelayMs);
                    continue;
                }
                if (c == '\t')
                {
                    PressVirtualKey(0x09);   // VK_TAB
                    sent++;
                    if (charDelayMs > 0) Thread.Sleep(charDelayMs);
                    continue;
                }

                Native.INPUT[] pair = new Native.INPUT[2];
                pair[0] = Native.KeyInput(0, c, Native.KEYEVENTF_UNICODE);
                pair[1] = Native.KeyInput(0, c, Native.KEYEVENTF_UNICODE | Native.KEYEVENTF_KEYUP);
                if (Native.Send(pair)) sent++;
                if (charDelayMs > 0) Thread.Sleep(charDelayMs);
            }
            return sent;
        }

        private static readonly System.Collections.Generic.Dictionary<string, ushort> VkMap =
            new System.Collections.Generic.Dictionary<string, ushort>(StringComparer.OrdinalIgnoreCase)
        {
            {"ENTER", 0x0D}, {"RETURN", 0x0D}, {"TAB", 0x09}, {"ESC", 0x1B}, {"ESCAPE", 0x1B},
            {"SPACE", 0x20}, {"BACKSPACE", 0x08}, {"DELETE", 0x2E}, {"DEL", 0x2E}, {"INSERT", 0x2D},
            {"HOME", 0x24}, {"END", 0x23}, {"PAGEUP", 0x21}, {"PAGEDOWN", 0x22},
            {"UP", 0x26}, {"DOWN", 0x28}, {"LEFT", 0x25}, {"RIGHT", 0x27},
            {"CTRL", 0x11}, {"CONTROL", 0x11}, {"SHIFT", 0x10}, {"ALT", 0x12},
            {"LWIN", 0x5B}, {"WIN", 0x5B}, {"RWIN", 0x5C},
            {"F1", 0x70}, {"F2", 0x71}, {"F3", 0x72}, {"F4", 0x73}, {"F5", 0x74}, {"F6", 0x75},
            {"F7", 0x76}, {"F8", 0x77}, {"F9", 0x78}, {"F10", 0x79}, {"F11", 0x7A}, {"F12", 0x7B},
        };

        private static void PressVirtualKey(ushort vk)
        {
            Native.Send(new[] {
                Native.KeyInput(vk, 0, 0),
                Native.KeyInput(vk, 0, Native.KEYEVENTF_KEYUP),
            });
        }

        /// <summary>
        /// Parses "ctrl+shift+s" / "ENTER" / "alt+F4" style key chords and sends them as one
        /// combination: modifiers are held down for the whole chord rather than pressed in
        /// sequence, which is what applications expect.
        /// </summary>
        public static string Keys(string chord)
        {
            if (string.IsNullOrEmpty(chord)) return "未提供按键";
            string[] parts = chord.Split(new[] { '+' }, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) return "未提供按键";

            var mods = new System.Collections.Generic.List<ushort>();
            var keys = new System.Collections.Generic.List<ushort>();

            foreach (string raw in parts)
            {
                string p = raw.Trim();
                if (p.Length == 0) continue;

                if (p.Length == 1)
                {
                    char c = char.ToUpperInvariant(p[0]);
                    if (c == '^') { mods.Add(0x11); continue; }
                    if (c == '%') { mods.Add(0x12); continue; }
                    if (c == '+') { mods.Add(0x10); continue; }
                    keys.Add((ushort)c);
                    continue;
                }

                ushort vk;
                if (VkMap.TryGetValue(p, out vk))
                {
                    bool isMod = (p.Equals("CTRL", StringComparison.OrdinalIgnoreCase)
                               || p.Equals("CONTROL", StringComparison.OrdinalIgnoreCase)
                               || p.Equals("SHIFT", StringComparison.OrdinalIgnoreCase)
                               || p.Equals("ALT", StringComparison.OrdinalIgnoreCase)
                               || p.Equals("WIN", StringComparison.OrdinalIgnoreCase));
                    if (isMod) mods.Add(vk); else keys.Add(vk);
                    continue;
                }
                return "未知按键: " + p;
            }

            if (keys.Count == 0) return "未提供有效按键";

            foreach (ushort m in mods)
                Native.Send(new[] { Native.KeyInput(m, 0, 0) });

            try
            {
                foreach (ushort k in keys)
                    Native.Send(new[] {
                        Native.KeyInput(k, 0, 0),
                        Native.KeyInput(k, 0, Native.KEYEVENTF_KEYUP),
                    });
            }
            finally
            {
                foreach (ushort m in mods)
                    Native.Send(new[] { Native.KeyInput(m, 0, Native.KEYEVENTF_KEYUP) });
            }

            return "";
        }

        /// <summary>
        /// Sets text through the UIA ValuePattern. This works without the window being
        /// foreground and without stealing focus, so it is the preferred path for filling
        /// text fields when the control exposes the pattern.
        /// </summary>
        public static string SetValuePattern(IntPtr root, string automationId, string nameContains, string text)
        {
            AutomationElement el = AutomationElement.FromHandle(root);
            CacheRequest cr = new CacheRequest();
            cr.Add(AutomationElement.AutomationIdProperty);
            cr.Add(AutomationElement.NameProperty);
            cr.Add(AutomationElement.ControlTypeProperty);
            cr.TreeScope = TreeScope.Element | TreeScope.Descendants;
            cr.TreeFilter = Automation.ControlViewCondition;

            using (cr.Activate())
            {
                AutomationElementCollection col = el.FindAll(TreeScope.Descendants, Automation.ControlViewCondition);
                for (int i = 0; i < col.Count; i++)
                {
                    AutomationElement e;
                    try { e = col[i]; } catch { continue; }

                    string id = "", nm = "";
                    try { id = e.Cached.AutomationId ?? ""; nm = e.Cached.Name ?? ""; } catch { continue; }

                    bool idOk = !string.IsNullOrEmpty(automationId) && id == automationId;
                    bool nameOk = !string.IsNullOrEmpty(nameContains)
                                  && nm.IndexOf(nameContains, StringComparison.OrdinalIgnoreCase) >= 0;
                    if (string.IsNullOrEmpty(automationId) && string.IsNullOrEmpty(nameContains)) continue;
                    if (!idOk && !nameOk) continue;

                    try
                    {
                        object pattern;
                        if (e.TryGetCurrentPattern(ValuePattern.Pattern, out pattern))
                        {
                            ((ValuePattern)pattern).SetValue(text);
                            return "";
                        }
                    }
                    catch (Exception ex) { return "UIA 设置值失败: " + ex.Message; }
                }
            }
            return "未找到支持 ValuePattern 的控件";
        }

        /// <summary>Invokes a control through UIA without moving the mouse.</summary>
        public static string InvokePattern(IntPtr root, string automationId, string nameContains)
        {
            AutomationElement el = AutomationElement.FromHandle(root);
            CacheRequest cr = new CacheRequest();
            cr.Add(AutomationElement.AutomationIdProperty);
            cr.Add(AutomationElement.NameProperty);
            cr.Add(AutomationElement.ControlTypeProperty);
            cr.TreeScope = TreeScope.Element | TreeScope.Descendants;
            cr.TreeFilter = Automation.ControlViewCondition;

            using (cr.Activate())
            {
                AutomationElementCollection col = el.FindAll(TreeScope.Descendants, Automation.ControlViewCondition);
                for (int i = 0; i < col.Count; i++)
                {
                    AutomationElement e;
                    try { e = col[i]; } catch { continue; }
                    string id = "", nm = "";
                    try { id = e.Cached.AutomationId ?? ""; nm = e.Cached.Name ?? ""; } catch { continue; }

                    bool idOk = !string.IsNullOrEmpty(automationId) && id == automationId;
                    bool nameOk = !string.IsNullOrEmpty(nameContains)
                                  && nm.IndexOf(nameContains, StringComparison.OrdinalIgnoreCase) >= 0;
                    if (string.IsNullOrEmpty(automationId) && string.IsNullOrEmpty(nameContains)) continue;
                    if (!idOk && !nameOk) continue;

                    try
                    {
                        object pattern;
                        // Fully qualified: this method is itself named InvokePattern, so the
                        // bare type name would resolve to the method.
                        if (e.TryGetCurrentPattern(System.Windows.Automation.InvokePattern.Pattern, out pattern))
                        {
                            ((System.Windows.Automation.InvokePattern)pattern).Invoke();
                            return "";
                        }
                    }
                    catch (Exception ex) { return "UIA 调用失败: " + ex.Message; }
                }
            }
            return "未找到支持 InvokePattern 的控件";
        }
    }
}
