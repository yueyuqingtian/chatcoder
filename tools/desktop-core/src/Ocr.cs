// OCR and text localization via the in-box Windows OCR engine.
//
// WHY REFLECTION: the strongly-typed WinRT projections (Windows.Media.Ocr) need the
// Windows SDK metadata (Windows.winmd) at COMPILE time, which is not installed on a stock
// machine. Late-bound activation through "Windows, ContentType=WindowsRuntime" works at
// RUNTIME without the SDK, which is why this file has no `using Windows.*`.
//
// The engine is in-box on every Windows 10/11 install (a language pack may be required for
// non-English recognition), so there is no model download and no third-party dependency
// such as Tesseract.
//
// This matters because some applications draw their own UI and expose an empty UI Automation
// tree — screenshots plus text localization is the only way to locate anything in them.

using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;

namespace DesktopCore
{
    internal sealed class TextWord
    {
        public string Text = "";
        public int X, Y, W, H;
    }

    internal static class Ocr
    {
        private static object _engine;
        private static bool _engineReady;
        private static string _engineInfo = "";
        private static MethodInfo _asTaskGeneric;
        private static bool _awaitReady;

        private static Type WinRt(string typeName)
        {
            return Type.GetType(typeName + ", Windows, ContentType=WindowsRuntime");
        }

        private static void InitAwait()
        {
            if (_awaitReady) return;
            // Full strong name is required: Assembly.Load("System.Runtime.WindowsRuntime")
            // throws FileNotFoundException because the simple name is not resolvable from
            // this assembly's context. Verified against the machine's actual GAC identity.
            Assembly rt = Assembly.Load(
                "System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, "
                + "PublicKeyToken=b77a5c561934e089");
            Type ext = rt.GetType("System.WindowsRuntimeSystemExtensions");
            if (ext == null) throw new InvalidOperationException("找不到 WinRT 异步桥接程序集。");
            foreach (MethodInfo m in ext.GetMethods())
            {
                if (m.Name == "AsTask" && m.GetParameters().Length == 1
                    && m.GetParameters()[0].ParameterType.Name == "IAsyncOperation`1")
                {
                    _asTaskGeneric = m;
                    break;
                }
            }
            if (_asTaskGeneric == null) throw new InvalidOperationException("找不到 AsTask 桥接方法。");
            _awaitReady = true;
        }

        /// <summary>
        /// Blocks until a WinRT IAsyncOperation completes and returns its result.
        ///
        /// IMPORTANT: the value returned by a WinRT factory is a bare System.__ComObject —
        /// it implements no visible interfaces, so reflecting on the VALUE cannot recover the
        /// result type. The only reliable source is the declaring MethodInfo's return type,
        /// e.g. IAsyncOperation&lt;StorageFile&gt;, which is why callers pass it in.
        /// </summary>
        private static object Await(object operation, Type resultType)
        {
            InitAwait();
            if (resultType == null)
                throw new InvalidOperationException("缺少异步操作的结果类型（应取自方法签名）。");

            MethodInfo m = _asTaskGeneric.MakeGenericMethod(resultType);
            object task = m.Invoke(null, new object[] { operation });
            task.GetType().GetMethod("Wait", new Type[] { typeof(int) }).Invoke(task, new object[] { -1 });
            return task.GetType().GetProperty("Result").GetValue(task, null);
        }

        /// <summary>Extract T from a method whose return type is IAsyncOperation&lt;T&gt;.</summary>
        private static Type AsyncResultOf(MethodInfo method)
        {
            if (method == null) return null;
            Type rt = method.ReturnType;
            if (rt != null && rt.IsGenericType
                && rt.GetGenericTypeDefinition().Name == "IAsyncOperation`1")
                return rt.GetGenericArguments()[0];
            return null;
        }

        /// <summary>Create (once) an OCR engine, preferring Chinese and falling back gracefully.</summary>
        private static object Engine()
        {
            if (_engineReady) return _engine;
            _engineReady = true;

            Type oeType = WinRt("Windows.Media.Ocr.OcrEngine");
            if (oeType == null) { _engineInfo = "系统不支持 Windows OCR。"; return null; }

            MethodInfo tryFromLang = oeType.GetMethod("TryCreateFromLanguage", BindingFlags.Public | BindingFlags.Static);
            MethodInfo tryFromProfile = oeType.GetMethod("TryCreateFromUserProfileLanguages", BindingFlags.Public | BindingFlags.Static);
            Type langType = WinRt("Windows.Globalization.Language");

            // Preferred order: Simplified Chinese (this product's primary locale), then
            // whatever the user profile offers, then any installed recognizer at all.
            string[] preferred = new[] { "zh-Hans-CN", "zh-CN", "en-US" };
            foreach (string tag in preferred)
            {
                if (tryFromLang == null || langType == null) break;
                try
                {
                    object lang = Activator.CreateInstance(langType, new object[] { tag });
                    object eng = tryFromLang.Invoke(null, new object[] { lang });
                    if (eng != null)
                    {
                        _engine = eng;
                        _engineInfo = tag;
                        return _engine;
                    }
                }
                catch { }
            }

            if (tryFromProfile != null)
            {
                try
                {
                    object eng = tryFromProfile.Invoke(null, null);
                    if (eng != null)
                    {
                        _engine = eng;
                        _engineInfo = "user-profile";
                        return _engine;
                    }
                }
                catch { }
            }

            _engineInfo = "未找到可用的 OCR 识别器（可能缺少语言包）。请安装「中文(简体)」语言包。";
            return null;
        }

        /// <summary>Run OCR on a PNG/JPEG file and return recognized words with pixel boxes.</summary>
        public static List<TextWord> Recognize(string imagePath)
        {
            List<TextWord> words = new List<TextWord>();
            object engine = Engine();
            if (engine == null) throw new InvalidOperationException(_engineInfo);

            Type sfType = WinRt("Windows.Storage.StorageFile");
            Type bdType = WinRt("Windows.Graphics.Imaging.BitmapDecoder");
            if (sfType == null || bdType == null) throw new InvalidOperationException("WinRT 图像类型不可用。");

            // StorageFile.GetFileFromPathAsync(path)
            MethodInfo getFile = null;
            foreach (MethodInfo m in sfType.GetMethods(BindingFlags.Public | BindingFlags.Static))
                if (m.Name == "GetFileFromPathAsync") { getFile = m; break; }
            if (getFile == null) throw new InvalidOperationException("找不到 GetFileFromPathAsync。");

            object fileOp = getFile.Invoke(null, new object[] { imagePath });
            object file = Await(fileOp, AsyncResultOf(getFile));

            // file.OpenAsync(FileAccessMode.Read)
            Type famType = WinRt("Windows.Storage.FileAccessMode");
            object readMode = famType == null ? null : Enum.Parse(famType, "Read");
            object stream = null;
            foreach (MethodInfo m in sfType.GetMethods())
            {
                if (m.Name != "OpenAsync") continue;
                ParameterInfo[] ps = m.GetParameters();
                if (ps.Length != 1) continue;
                object arg = readMode != null && ps[0].ParameterType == famType ? readMode : (object)0;
                object op = m.Invoke(file, new object[] { arg });
                stream = Await(op, AsyncResultOf(m));
                break;
            }
            if (stream == null) throw new InvalidOperationException("无法打开图像流。");

            // BitmapDecoder.CreateAsync(stream) -> GetSoftwareBitmapAsync()
            MethodInfo create = null;
            foreach (MethodInfo m in bdType.GetMethods(BindingFlags.Public | BindingFlags.Static))
                if (m.Name == "CreateAsync") { create = m; break; }
            if (create == null) throw new InvalidOperationException("找不到 BitmapDecoder.CreateAsync。");

            object decOp = create.Invoke(null, new object[] { stream });
            object decoder = Await(decOp, AsyncResultOf(create));

            MethodInfo getBmp = null;
            foreach (MethodInfo m in bdType.GetMethods())
                if (m.Name == "GetSoftwareBitmapAsync" && m.GetParameters().Length == 0) { getBmp = m; break; }
            if (getBmp == null) throw new InvalidOperationException("找不到 GetSoftwareBitmapAsync。");

            object bmpOp = getBmp.Invoke(decoder, null);
            object softwareBitmap = Await(bmpOp, AsyncResultOf(getBmp));

            // engine.RecognizeAsync(softwareBitmap)
            MethodInfo recognize = null;
            foreach (MethodInfo m in engine.GetType().GetMethods())
                if (m.Name == "RecognizeAsync" && m.GetParameters().Length == 1) { recognize = m; break; }
            if (recognize == null) throw new InvalidOperationException("找不到 RecognizeAsync。");

            object recOp = recognize.Invoke(engine, new object[] { softwareBitmap });
            object result = Await(recOp, AsyncResultOf(recognize));

            // Walk result.Lines -> line.Words -> word.BoundingRect {X,Y,Width,Height}
            object lines = result.GetType().GetProperty("Lines").GetValue(result, null);
            if (lines != null)
            {
                System.Collections.IEnumerable lineEnum = lines as System.Collections.IEnumerable;
                if (lineEnum != null)
                {
                    foreach (object line in lineEnum)
                    {
                        object lineWords = line.GetType().GetProperty("Words").GetValue(line, null);
                        System.Collections.IEnumerable wordEnum = lineWords as System.Collections.IEnumerable;
                        if (wordEnum == null) continue;

                        foreach (object w in wordEnum)
                        {
                            Type wt = w.GetType();
                            TextWord tw = new TextWord();
                            tw.Text = Convert.ToString(wt.GetProperty("Text").GetValue(w, null)) ?? "";
                            object rect = wt.GetProperty("BoundingRect").GetValue(w, null);
                            if (rect != null)
                            {
                                Type rt = rect.GetType();
                                tw.X = Convert.ToInt32(rt.GetProperty("X").GetValue(rect, null));
                                tw.Y = Convert.ToInt32(rt.GetProperty("Y").GetValue(rect, null));
                                tw.W = Convert.ToInt32(rt.GetProperty("Width").GetValue(rect, null));
                                tw.H = Convert.ToInt32(rt.GetProperty("Height").GetValue(rect, null));
                            }
                            words.Add(tw);
                        }
                    }
                }
            }

            try { result.GetType().GetMethod("Dispose").Invoke(result, null); } catch { }
            try { softwareBitmap.GetType().GetMethod("Dispose").Invoke(softwareBitmap, null); } catch { }

            return words;
        }

        /// <summary>
        /// Locate a piece of text on screen and return clickable screen coordinates.
        /// This replaces "screenshot -> eyeball the coordinates -> click -> screenshot to check".
        ///
        /// Coordinates are mapped back through the capture's downscale factor AND its origin,
        /// so a region capture returns absolute screen coordinates too.
        /// </summary>
        public static string FindText(string text, bool exact, string region,
                                      int maxDim, int maxCandidates, int srcX0, int srcY0)
        {
            if (string.IsNullOrEmpty(text))
                return "{\"error\":\"text 不能为空\"}";

            int x = 0, y = 0, w = 0, h = 0;
            if (!string.IsNullOrEmpty(region))
            {
                string[] parts = region.Split(',');
                if (parts.Length == 4)
                {
                    int.TryParse(parts[0], out x);
                    int.TryParse(parts[1], out y);
                    int.TryParse(parts[2], out w);
                    int.TryParse(parts[3], out h);
                }
            }

            string tmp = Path.Combine(Path.GetTempPath(),
                "desktop_core_ocr_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".png");

            string shotJson;
            try
            {
                // PNG keeps glyph edges crisp, which measurably improves recognition.
                shotJson = Sensing.Capture(tmp, x, y, w, h, maxDim, "png", 100);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"截图失败: " + Js.Esc(ex.Message) + "\"}";
            }

            // Parse the capture metadata to recover origin + scale for coordinate mapping.
            int capturedW = Js.GetInt(shotJson, "width", 0);
            int capturedH = Js.GetInt(shotJson, "height", 0);
            double scale = 1.0;
            string scaleRaw = Js.GetStr(shotJson, "scale", "1");
            double.TryParse(scaleRaw, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out scale);
            if (scale <= 0) scale = 1.0;
            int originX = Js.GetInt(shotJson, "srcX", 0);
            int originY = Js.GetInt(shotJson, "srcY", 0);

            List<TextWord> words;
            try
            {
                words = Recognize(tmp);
            }
            catch (Exception ex)
            {
                try { File.Delete(tmp); } catch { }
                return "{\"error\":\"" + Js.Esc(ex.Message) + "\"}";
            }
            finally
            {
                try { File.Delete(tmp); } catch { }
            }

            string needle = exact ? text : text.ToLowerInvariant();

            // Merge adjacent words on the same line so multi-word targets ("薛之谦 演员")
            // can be located as one clickable unit instead of two separate boxes.
            List<TextWord> matches = new List<TextWord>();
            StringBuilder lineText = new StringBuilder();
            List<TextWord> lineWords = new List<TextWord>();
            int lastY = int.MinValue;

            Action flush = () =>
            {
                if (lineWords.Count == 0) return;
                string joined = lineText.ToString();
                string hay = exact ? joined : joined.ToLowerInvariant();
                if (hay.Contains(needle))
                {
                    int minX = int.MaxValue, minY = int.MaxValue, maxR = int.MinValue, maxB = int.MinValue;
                    foreach (TextWord lw in lineWords)
                    {
                        if (lw.X < minX) minX = lw.X;
                        if (lw.Y < minY) minY = lw.Y;
                        if (lw.X + lw.W > maxR) maxR = lw.X + lw.W;
                        if (lw.Y + lw.H > maxB) maxB = lw.Y + lw.H;
                    }
                    TextWord merged = new TextWord();
                    merged.Text = joined;
                    merged.X = minX; merged.Y = minY;
                    merged.W = maxR - minX; merged.H = maxB - minY;
                    matches.Add(merged);
                }
                lineText.Length = 0;
                lineWords.Clear();
            };

            foreach (TextWord tw in words)
            {
                if (lastY != int.MinValue && Math.Abs(tw.Y - lastY) > Math.Max(6, tw.H / 2))
                {
                    flush();
                }
                if (lineText.Length > 0) lineText.Append(' ');
                lineText.Append(tw.Text);
                lineWords.Add(tw);
                lastY = tw.Y;
            }
            flush();

            if (matches.Count == 0)
            {
                // Report what IS on screen so the caller can correct the target text
                // without taking another screenshot.
                StringBuilder sample = new StringBuilder();
                int shown = 0;
                foreach (TextWord tw in words)
                {
                    if (shown++ >= 40) break;
                    if (sample.Length > 0) sample.Append(' ');
                    sample.Append(tw.Text);
                }
                return "{\"found\":false,\"hint\":\"屏幕上未找到该文字\",\"visible\":\""
                     + Js.Esc(sample.ToString()) + "\"}";
            }

            matches.Sort((a, b) => b.W.CompareTo(a.W));

            StringBuilder sb = new StringBuilder(1024);
            sb.Append("{\"found\":true,\"count\":").Append(matches.Count);
            int emitted = 0;
            for (int i = 0; i < matches.Count && emitted < Math.Max(1, maxCandidates); i++)
            {
                TextWord m = matches[i];
                // Image pixel -> screen pixel: divide by the downscale, then add the origin.
                int cx = originX + (int)Math.Round((m.X + m.W / 2.0) / scale);
                int cy = originY + (int)Math.Round((m.Y + m.H / 2.0) / scale);
                if (emitted == 0)
                {
                    sb.Append(",\"x\":").Append(cx).Append(",\"y\":").Append(cy)
                      .Append(",\"text\":\"").Append(Js.Esc(m.Text)).Append('"');
                }
                if (emitted > 0) sb.Append(',');
                else if (emitted == 0) sb.Append(",\"candidates\":[");
                sb.Append("{\"text\":\"").Append(Js.Esc(m.Text))
                  .Append("\",\"x\":").Append(cx).Append(",\"y\":").Append(cy)
                  .Append(",\"w\":").Append((int)Math.Round(m.W / scale))
                  .Append(",\"h\":").Append((int)Math.Round(m.H / scale)).Append('}');
                emitted++;
            }
            if (emitted > 0) sb.Append(']');
            sb.Append(",\"imageSize\":[").Append(capturedW).Append(',').Append(capturedH).Append("]}");
            return sb.ToString();
        }
    }
}
