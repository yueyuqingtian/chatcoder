// Application discovery and launching.
//
// Finding an installed program is a surprisingly hard problem on Windows: display names are
// localized (an English keyword never matches "QQ音乐"), many apps are not on PATH, and
// portable installs live in arbitrary directories. This walks the same sources a human would,
// in order of reliability:
//   1. Uninstall registry entries, matched on BOTH display name and key name (localization)
//   2. App Paths (what the shell uses for `start foo`)
//   3. PATH
//   4. Start Menu shortcuts
// Results are ranked so the most likely executable comes first.

using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Microsoft.Win32;

namespace DesktopCore
{
    internal sealed class AppHit
    {
        public string Name = "";
        public string Exe = "";
        public int Score;
    }

    internal static class Apps
    {
        public static List<AppHit> Find(string keyword, int limit)
        {
            List<AppHit> hits = new List<AppHit>();
            if (string.IsNullOrEmpty(keyword)) return hits;
            string kw = keyword.Trim().ToLowerInvariant();

            SearchUninstall(hits, kw);
            SearchAppPaths(hits, kw);
            SearchStartMenu(hits, kw);

            // Rank: exact-ish directory/name matches first, then shorter paths.
            hits.Sort((a, b) =>
            {
                int c = b.Score.CompareTo(a.Score);
                if (c != 0) return c;
                return a.Exe.Length.CompareTo(b.Exe.Length);
            });

            // De-duplicate by executable path.
            List<AppHit> unique = new List<AppHit>();
            HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (AppHit h in hits)
            {
                if (seen.Add(h.Exe)) unique.Add(h);
                if (unique.Count >= limit) break;
            }
            return unique;
        }

        private static void Add(List<AppHit> hits, string name, string exe, int score)
        {
            if (string.IsNullOrEmpty(exe)) return;
            if (!exe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) return;
            try { if (!File.Exists(exe)) return; } catch { return; }
            AppHit h = new AppHit();
            h.Name = name;
            h.Exe = exe;
            h.Score = score;
            hits.Add(h);
        }

        private static void SearchUninstall(List<AppHit> hits, string kw)
        {
            string[] roots = new[]
            {
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                @"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            };
            foreach (string rootPath in roots)
            {
                try
                {
                    using (RegistryKey root = Registry.LocalMachine.OpenSubKey(rootPath))
                    {
                        if (root == null) continue;
                        foreach (string sub in root.GetSubKeyNames())
                        {
                            using (RegistryKey k = root.OpenSubKey(sub))
                            {
                                if (k == null) continue;
                                // Match the KEY name too: DisplayName is localized, so an
                                // English keyword ("qqmusic") would never match "QQ音乐".
                                bool keyMatch = sub.ToLowerInvariant().Contains(kw);
                                string display = Convert.ToString(k.GetValue("DisplayName")) ?? "";
                                bool nameMatch = display.ToLowerInvariant().Contains(kw);
                                if (!keyMatch && !nameMatch) continue;

                                string icon = Convert.ToString(k.GetValue("DisplayIcon")) ?? "";
                                if (icon.Length > 0)
                                {
                                    int comma = icon.IndexOf(',');
                                    if (comma > 0) icon = icon.Substring(0, comma);
                                    icon = icon.Trim('"');
                                    Add(hits, string.IsNullOrEmpty(display) ? sub : display, icon,
                                        nameMatch ? 90 : 70);
                                }

                                string loc = Convert.ToString(k.GetValue("InstallLocation")) ?? "";
                                if (loc.Length > 0)
                                {
                                    try
                                    {
                                        foreach (string f in Directory.GetFiles(loc, "*.exe", SearchOption.TopDirectoryOnly))
                                        {
                                            string fn = Path.GetFileNameWithoutExtension(f).ToLowerInvariant();
                                            if (fn.Contains(kw) || sub.ToLowerInvariant().Contains(fn))
                                                Add(hits, Path.GetFileNameWithoutExtension(f), f, 60);
                                        }
                                    }
                                    catch { }
                                }
                            }
                        }
                    }
                }
                catch { }
            }
        }

        private static void SearchAppPaths(List<AppHit> hits, string kw)
        {
            string[] roots = new[]
            {
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                @"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
            };
            foreach (string rootPath in roots)
            {
                try
                {
                    using (RegistryKey root = Registry.LocalMachine.OpenSubKey(rootPath))
                    {
                        if (root == null) continue;
                        foreach (string sub in root.GetSubKeyNames())
                        {
                            if (!sub.ToLowerInvariant().Contains(kw)) continue;
                            using (RegistryKey k = root.OpenSubKey(sub))
                            {
                                if (k == null) continue;
                                string exe = Convert.ToString(k.GetValue("")) ?? "";
                                if (exe.Length == 0)
                                {
                                    string p = Convert.ToString(k.GetValue("Path")) ?? "";
                                    if (p.Length > 0) exe = Path.Combine(p, sub);
                                }
                                Add(hits, Path.GetFileNameWithoutExtension(sub), exe.Trim('"'), 95);
                            }
                        }
                    }
                }
                catch { }
            }
        }

        private static void SearchStartMenu(List<AppHit> hits, string kw)
        {
            List<string> roots = new List<string>();
            try
            {
                string common = Environment.GetFolderPath(Environment.SpecialFolder.CommonStartMenu);
                if (!string.IsNullOrEmpty(common)) roots.Add(Path.Combine(common, "Programs"));
                string user = Environment.GetFolderPath(Environment.SpecialFolder.StartMenu);
                if (!string.IsNullOrEmpty(user)) roots.Add(Path.Combine(user, "Programs"));
            }
            catch { }

            foreach (string root in roots)
            {
                if (!Directory.Exists(root)) continue;
                try
                {
                    foreach (string lnk in Directory.GetFiles(root, "*.lnk", SearchOption.AllDirectories))
                    {
                        string name = Path.GetFileNameWithoutExtension(lnk);
                        if (name.ToLowerInvariant().Contains(kw))
                        {
                            // Resolving .lnk targets needs shell interop; the shortcut name is
                            // still useful evidence, so record it without an exe path.
                            Add(hits, name, ResolveShortcut(lnk), 80);
                        }
                    }
                }
                catch { }
            }
        }

        /// <summary>
        /// Resolve a .lnk target through WScript.Shell (late-bound COM, no extra reference).
        /// Returns "" when the shell cannot resolve it, in which case the entry is dropped.
        /// </summary>
        private static string ResolveShortcut(string lnk)
        {
            try
            {
                Type t = Type.GetTypeFromProgID("WScript.Shell");
                if (t == null) return "";
                object shell = Activator.CreateInstance(t);
                try
                {
                    object sc = t.InvokeMember("CreateShortcut", System.Reflection.BindingFlags.InvokeMethod,
                        null, shell, new object[] { lnk });
                    if (sc == null) return "";
                    Type st = sc.GetType();
                    object target = st.InvokeMember("TargetPath", System.Reflection.BindingFlags.GetProperty,
                        null, sc, null);
                    return Convert.ToString(target) ?? "";
                }
                finally
                {
                    try { System.Runtime.InteropServices.Marshal.ReleaseComObject(shell); } catch { }
                }
            }
            catch { return ""; }
        }

        /// <summary>
        /// Launch a program and wait for a window of that process to appear.
        /// Returns the window info so the caller can act immediately without another probe.
        /// </summary>
        public static string Start(string exe, string keyword, int timeoutMs)
        {
            string target = exe;

            if (string.IsNullOrEmpty(target) && !string.IsNullOrEmpty(keyword))
            {
                List<AppHit> hits = Find(keyword, 5);
                if (hits.Count > 0) target = hits[0].Exe;
            }
            if (string.IsNullOrEmpty(target))
                return "{\"error\":\"未找到要启动的程序\"}";
            if (!File.Exists(target))
                return "{\"error\":\"可执行文件不存在: " + Js.Esc(target) + "\"}";

            System.Diagnostics.Process proc;
            try
            {
                System.Diagnostics.ProcessStartInfo psi = new System.Diagnostics.ProcessStartInfo(target);
                psi.UseShellExecute = true;
                psi.WorkingDirectory = Path.GetDirectoryName(target);
                proc = System.Diagnostics.Process.Start(psi);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"启动失败: " + Js.Esc(ex.Message) + "\"}";
            }

            if (proc == null)
                return "{\"error\":\"启动未返回进程句柄\"}";

            // Poll for a real top-level window rather than sleeping a fixed amount.
            IntPtr found = IntPtr.Zero;
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs <= 0 ? 8000 : timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                System.Threading.Thread.Sleep(150);
                try { proc.Refresh(); } catch { }
                uint pid = (uint)proc.Id;
                Native.EnumWindows((h, lp) =>
                {
                    if (!Native.IsWindowVisible(h)) return true;
                    uint wp;
                    Native.GetWindowThreadProcessId(h, out wp);
                    if (wp != pid) return true;
                    StringBuilder t = new StringBuilder(512);
                    Native.GetWindowTextW(h, t, 512);
                    if (t.Length == 0) return true;
                    found = h;
                    return false;
                }, IntPtr.Zero);
                if (found != IntPtr.Zero) break;
            }

            if (found == IntPtr.Zero)
                return "{\"exe\":\"" + Js.Esc(target) + "\",\"pid\":" + proc.Id + ",\"handle\":0}";

            StringBuilder title = new StringBuilder(512);
            Native.GetWindowTextW(found, title, 512);
            Native.RECT r;
            Native.GetWindowRect(found, out r);
            return "{\"exe\":\"" + Js.Esc(target) + "\",\"pid\":" + proc.Id
                 + ",\"handle\":" + found.ToInt64()
                 + ",\"title\":\"" + Js.Esc(title.ToString()) + "\""
                 + ",\"rect\":[" + r.Left + "," + r.Top + "," + (r.Right - r.Left) + "," + (r.Bottom - r.Top) + "]"
                 + ",\"x\":" + (r.Left + (r.Right - r.Left) / 2)
                 + ",\"y\":" + (r.Top + (r.Bottom - r.Top) / 2) + "}";
        }
    }
}
