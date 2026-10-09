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

        // ── OCR 结果缓存（plan-365-1774）──
        //
        // 为什么需要：Windows OCR 对整屏图耗时 300-400 ms（实测），而调用方「截图时识别一次
        // 文字块做编号」与「随后按文字定位/点击」看的常是同一画面——重复识别纯属浪费。
        //
        // 键取**图片内容哈希**而不是路径：同一画面即使换了文件名（每次截图都落在新文件）
        // 也能命中；画面一变内容哈希就变，天然不会读到过期结果。
        private const int CacheLimit = 8;
        private static readonly object _cacheLock = new object();
        private static readonly Dictionary<string, OcrCacheEntry> _cache =
            new Dictionary<string, OcrCacheEntry>(StringComparer.Ordinal);
        private static readonly List<string> _cacheOrder = new List<string>();

        private sealed class OcrCacheEntry
        {
            public List<TextWord> Words;
        }

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

        /// <summary>Run OCR on a PNG/JPEG file and return recognized words with pixel boxes.
        ///
        /// 命中内容缓存时直接返回（plan-365-1774）：同一张图的 ocrboxes 与 findtext 共用
        /// 一次识别结果，模型连查多个词时不再重复付 OCR 的 300-400 ms；缓存以内容哈希
        /// 为键，同一画面即使每次截图落在新文件名上也能命中。</summary>
        public static List<TextWord> Recognize(string imagePath, bool allowCache)
        {
            if (allowCache)
            {
                List<TextWord> cached;
                if (TryGetCached(imagePath, out cached)) return cached;
            }

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

            if (allowCache) PutCached(imagePath, words);
            return words;
        }

        /// <summary>取缓存：键是图片内容哈希（SHA1），与文件路径无关。</summary>
        private static bool TryGetCached(string imagePath, out List<TextWord> words)
        {
            words = null;
            string key = ContentKey(imagePath);
            if (key.Length == 0) return false;

            lock (_cacheLock)
            {
                OcrCacheEntry entry;
                if (!_cache.TryGetValue(key, out entry)) return false;
                _cacheOrder.Remove(key);
                _cacheOrder.Add(key);        // 最近使用：LRU 淘汰从头部开始
                words = new List<TextWord>(entry.Words);
                return true;
            }
        }

        /// <summary>写缓存；命中结果由只读遍历使用，调用方不得改写里面的词对象。</summary>
        private static void PutCached(string imagePath, List<TextWord> words)
        {
            if (words.Count == 0) return;
            string key = ContentKey(imagePath);
            if (key.Length == 0) return;

            OcrCacheEntry entry = new OcrCacheEntry();
            entry.Words = new List<TextWord>(words);   // 存副本：调用方后续对原列表的增删不影响缓存
            lock (_cacheLock)
            {
                _cache[key] = entry;
                _cacheOrder.Remove(key);
                _cacheOrder.Add(key);
                while (_cacheOrder.Count > CacheLimit)
                {
                    string oldest = _cacheOrder[0];
                    _cacheOrder.RemoveAt(0);
                    _cache.Remove(oldest);
                }
            }
        }

        /// <summary>图片内容哈希（SHA1 hex）；读不到文件返回空串（视为不可缓存）。</summary>
        private static string ContentKey(string imagePath)
        {
            if (string.IsNullOrEmpty(imagePath)) return "";
            try
            {
                using (FileStream fs = File.OpenRead(imagePath))
                using (System.Security.Cryptography.SHA1 sha = System.Security.Cryptography.SHA1.Create())
                {
                    byte[] hash = sha.ComputeHash(fs);
                    StringBuilder sb = new StringBuilder(hash.Length * 2);
                    foreach (byte b in hash) sb.Append(b.ToString("x2"));
                    return sb.ToString();
                }
            }
            catch
            {
                return "";
            }
        }

        /// <summary>
        /// Locate a piece of text on screen and return clickable screen coordinates.
        /// This replaces "screenshot -> eyeball the coordinates -> click -> screenshot to check".
        ///
        /// Coordinates are mapped back through the capture's downscale factor AND its origin,
        /// so a region capture returns absolute screen coordinates too.
        ///
        /// 返回的是**目标文字自己的框**（词级定位，plan-365-1773）：命中范围映射回词的并集
        /// 包围盒，而不是整行中心——菜单栏一行多个可点项时，整行中心会点到别的项上。
        /// 匹配前双方都去空白（中文 OCR 常在字间插空格），且命中不得跨越"按间距切分的
        /// 不同控件"（否则会把两个菜单项连起来点）。
        ///
        /// 复用已有截图（plan-365-1757 B3）：调用方刚截过图时把路径经 image 传进来，
        /// 直接在这张图上做 OCR，省掉一次全屏截图（实测约 110 ms）。
        /// 此时缩放比与原点是调用方已知的事实，必须一并传入（scale/srcX/srcY），内核不做猜测；
        /// 调用方的图片绝不删除，只有自己截的临时图才会清理。
        /// </summary>
        public static string FindText(string text, bool exact, string region, string image,
                                      double callerScale, int callerX, int callerY,
                                      int maxDim, int maxCandidates)
        {
            if (string.IsNullOrEmpty(text))
                return "{\"error\":\"text 不能为空\"}";

            OcrSource src = ResolveSource(region, image, callerScale, callerX, callerY, maxDim);
            if (src.Error.Length > 0)
                return "{\"error\":\"" + Js.Esc(src.Error) + "\"}";

            List<TextWord> words;
            try
            {
                words = Recognize(src.Path, true);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Js.Esc(ex.Message) + "\"}";
            }
            finally
            {
                src.Cleanup();
            }

            double scale = src.Scale;
            int originX = src.OriginX, originY = src.OriginY;

            // 词级定位（plan-365-1773 修复）：不再“按行合并后整行匹配”。
            // 旧实现把整行合成一个框——菜单栏这种一行十几个可点项的界面，
            // 命中「收费」返回的是整行中心（实测偏出 500px 以上，落在别的菜单项上）。
            string needle = Compact(text);

            List<TextWord> matches = new List<TextWord>();
            foreach (List<TextWord> line in GroupLines(words))
            {
                LocateInLine(line, needle, exact, matches);
            }

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
                     + Js.Esc(sample.ToString()) + "\""
                     + ",\"src\":\"" + src.Src + "\""
                     + ",\"scaleUsed\":" + scale.ToString("F6", System.Globalization.CultureInfo.InvariantCulture)
                     + ",\"origin\":[" + originX + "," + originY + "]"
                     + ",\"imageSize\":[" + src.Width + "," + src.Height + "]}";
            }

            // 按阅读顺序（上→下、左→右）排列：命中都是精确匹配，没有“宽度更可信”一说，
            // 顺序可预测才能让调用方/模型稳定地选择候选。
            matches.Sort((a, b) =>
            {
                int c = a.Y.CompareTo(b.Y);
                return c != 0 ? c : a.X.CompareTo(b.X);
            });

            StringBuilder sb = new StringBuilder(1024);
            sb.Append("{\"found\":true,\"count\":").Append(matches.Count);
            int emitted = 0;
            int rawX = 0, rawY = 0;
            for (int i = 0; i < matches.Count && emitted < Math.Max(1, maxCandidates); i++)
            {
                TextWord m = matches[i];
                // Image pixel -> screen pixel: divide by the downscale, then add the origin.
                int rx = (int)Math.Round(m.X + m.W / 2.0);
                int ry = (int)Math.Round(m.Y + m.H / 2.0);
                int cx = originX + (int)Math.Round(rx / scale);
                int cy = originY + (int)Math.Round(ry / scale);
                if (emitted == 0) { rawX = rx; rawY = ry; }
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
            sb.Append(",\"imageSize\":[").Append(src.Width).Append(',').Append(src.Height).Append(']')
              .Append(",\"src\":\"").Append(src.Src).Append('"')
              .Append(",\"scaleUsed\":").Append(scale.ToString("F6", System.Globalization.CultureInfo.InvariantCulture))
              .Append(",\"origin\":[").Append(originX).Append(',').Append(originY).Append(']')
              .Append(",\"rawXY\":[").Append(rawX).Append(',').Append(rawY).Append("]}");
            return sb.ToString();
        }

        /// <summary>
        /// 返回屏幕上全部**可点文字块**（plan-365-1757 A2 的第三类元素来源）。
        ///
        /// 用途：自绘界面（音乐/视频客户端等）既没有 UIA 控件树、也没有 CDP，模型只能靠看图
        /// 猜坐标——这正是点错最严重的一类界面。有了文字块，工具层就能给这些文字编号，
        /// 模型只需说「点 #7」。坐标已换算成屏幕物理像素，可直接用于点击。
        ///
        /// 按词间距切块（plan-365-1773）：旧实现把整行合并成一个框，菜单栏这种一行十几个
        /// 可点项的界面，编号框盖住整行、中心落在两个菜单项之间——点编号就是“点到按钮附近”。
        /// 现在间距超过约 0.8 字高的相邻词视为不同控件，各自成块。
        ///
        /// image/scale/srcX/srcY 可复用调用方已有的截图，避免为 OCR 再截一次屏（约 110 ms）。
        /// </summary>
        public static string Boxes(string region, string image, double callerScale,
                                   int callerX, int callerY, int maxDim, int maxBoxes)
        {
            OcrSource src = ResolveSource(region, image, callerScale, callerX, callerY, maxDim);
            if (src.Error.Length > 0)
                return "{\"error\":\"" + Js.Esc(src.Error) + "\"}";

            List<TextWord> words;
            try
            {
                words = Recognize(src.Path, true);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Js.Esc(ex.Message) + "\"}";
            }
            finally
            {
                src.Cleanup();
            }

            // 行内按词间距切块（plan-365-1773）：每块是一个可点单元，而不是整行或碎片词。
            List<TextWord> blocks = new List<TextWord>();
            foreach (List<TextWord> line in GroupLines(words))
            {
                blocks.AddRange(SplitBlocks(line));
            }
            if (maxBoxes <= 0) maxBoxes = 200;

            StringBuilder sb = new StringBuilder(4096);
            int n = 0;
            for (int i = 0; i < blocks.Count && n < maxBoxes; i++)
            {
                TextWord line = blocks[i];
                if (line.W < 2 || line.H < 2 || line.Text.Length == 0) continue;
                int left = src.OriginX + (int)Math.Round(line.X / src.Scale);
                int top = src.OriginY + (int)Math.Round(line.Y / src.Scale);
                int w = (int)Math.Round(line.W / src.Scale);
                int h = (int)Math.Round(line.H / src.Scale);
                if (n > 0) sb.Append(',');
                sb.Append("{\"t\":\"").Append(Js.Esc(line.Text))
                  .Append("\",\"x\":").Append(left).Append(",\"y\":").Append(top)
                  .Append(",\"w\":").Append(w).Append(",\"h\":").Append(h).Append('}');
                n++;
            }
            return "{\"count\":" + n + ",\"boxes\":[" + sb + "]"
                 + ",\"imageSize\":[" + src.Width + "," + src.Height + "]"
                 + ",\"src\":\"" + src.Src + "\""
                 + ",\"scaleUsed\":" + src.Scale.ToString("F6", System.Globalization.CultureInfo.InvariantCulture)
                 + ",\"origin\":[" + src.OriginX + "," + src.OriginY + "]}";
        }

        /// <summary>
        /// 一次 OCR 所需的「图片 + 坐标换算参数」。
        ///
        /// 单独抽出来，是因为这里有三个必须只有一份实现的易错点：
        /// ① 复用调用方图片时，缩放比与原点只能由调用方提供（内核无从得知）；
        /// ② Capture 返回的 scale 是**数字**，必须走数值读取（历史上用 GetStr 取，恒得 1.0，
        ///    导致降采样时坐标整体偏小 1/scale 倍——见 plan-365-1757 的修复说明）；
        /// ③ WinRT 的 StorageFile 只认反斜杠路径，传正斜杠会直接抛异常。
        /// </summary>
        private sealed class OcrSource
        {
            public string Path = "";
            public bool Own;
            public string Temp = "";
            public double Scale = 1.0;
            public int OriginX, OriginY;
            public int Width, Height;
            public string Error = "";

            public string Src { get { return Own ? "capture" : "reuse"; } }

            /// <summary>只清理自己截的临时图；调用方传进来的图片绝不能删。</summary>
            public void Cleanup()
            {
                if (Own && Temp.Length > 0)
                {
                    try { File.Delete(Temp); }
                    catch { }
                }
            }
        }

        private static void ParseRegion(string region, out int x, out int y, out int w, out int h)
        {
            x = 0; y = 0; w = 0; h = 0;
            if (string.IsNullOrEmpty(region)) return;
            string[] parts = region.Split(',');
            if (parts.Length != 4) return;
            int.TryParse(parts[0], out x);
            int.TryParse(parts[1], out y);
            int.TryParse(parts[2], out w);
            int.TryParse(parts[3], out h);
        }

        private static OcrSource ResolveSource(string region, string image, double callerScale,
                                               int callerX, int callerY, int maxDim)
        {
            OcrSource s = new OcrSource();
            s.Scale = callerScale > 0 ? callerScale : 1.0;
            s.OriginX = callerX;
            s.OriginY = callerY;

            string path = image;
            if (!string.IsNullOrEmpty(path)) path = path.Replace('/', '\\');
            s.Path = path;
            s.Own = string.IsNullOrEmpty(path);

            if (s.Own)
            {
                int x, y, w, h;
                ParseRegion(region, out x, out y, out w, out h);
                s.Temp = Path.Combine(Path.GetTempPath(),
                    "desktop_core_ocr_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".png");

                string shotJson;
                try
                {
                    // PNG keeps glyph edges crisp, which measurably improves recognition.
                    shotJson = Sensing.Capture(s.Temp, x, y, w, h, maxDim, "png", 100);
                }
                catch (Exception ex)
                {
                    s.Error = "截图失败: " + ex.Message;
                    return s;
                }

                s.Width = Js.GetInt(shotJson, "width", 0);
                s.Height = Js.GetInt(shotJson, "height", 0);
                s.Scale = Js.GetDouble(shotJson, "scale", 1.0);
                if (s.Scale <= 0) s.Scale = 1.0;
                s.OriginX = Js.GetInt(shotJson, "srcX", 0);
                s.OriginY = Js.GetInt(shotJson, "srcY", 0);
                s.Path = s.Temp;
            }
            else
            {
                if (!File.Exists(s.Path))
                {
                    s.Error = "复用图片不存在: " + s.Path;
                    return s;
                }
                // 尺寸从文件头读，仅用于回显；坐标换算只依赖调用方给的 scale/原点。
                try
                {
                    using (System.Drawing.Image img = System.Drawing.Image.FromFile(s.Path))
                    {
                        s.Width = img.Width;
                        s.Height = img.Height;
                    }
                }
                catch { }
            }
            return s;
        }

        /// <summary>
        /// 按视觉行分组（同行的词归在一起，保持识别顺序）。
        ///
        /// 与旧实现的区别（plan-365-1773）：**只分组，不合并成整行框**。
        /// 合并整行会让命中/编号的坐标退化为整行中心——菜单栏一行十几个可点项时，
        /// 整行中心落在两个菜单项之间，这就是「点到按钮附近」的直接来源。
        /// </summary>
        private static List<List<TextWord>> GroupLines(List<TextWord> words)
        {
            List<List<TextWord>> lines = new List<List<TextWord>>();
            List<TextWord> cur = new List<TextWord>();
            int lastY = int.MinValue;
            foreach (TextWord tw in words)
            {
                if (lastY != int.MinValue && Math.Abs(tw.Y - lastY) > Math.Max(6, tw.H / 2))
                {
                    if (cur.Count > 0) lines.Add(cur);
                    cur = new List<TextWord>();
                }
                cur.Add(tw);
                lastY = tw.Y;
            }
            if (cur.Count > 0) lines.Add(cur);
            return lines;
        }

        /// <summary>去掉所有空白：OCR 常把中文词拆开并在字间插空格（"收 费"），匹配前需归一。</summary>
        private static string Compact(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            StringBuilder sb = new StringBuilder(s.Length);
            foreach (char c in s)
            {
                if (!char.IsWhiteSpace(c)) sb.Append(c);
            }
            return sb.ToString();
        }

        /// <summary>
        /// 相邻词是否属于同一控件：水平间距不超过约 0.8 字高（最小 6px）。
        /// 阈值取 0.8 字高是因为词内字距通常 &lt; 0.2 字高，而不同控件（菜单项、工具条按钮）
        /// 之间一般留出 ≥ 1 字高的空隙，两侧都有足够余量，不会因字体大小而失灵。
        /// </summary>
        private static bool SameGroup(TextWord a, TextWord b)
        {
            int gap = b.X - (a.X + a.W);
            int h = Math.Max(a.H, b.H);
            int limit = Math.Max((int)(h * 0.8), 6);
            return gap <= limit;
        }

        /// <summary>取 [i0, i1] 词区间的并集包围盒（文本用空格连接，便于回显）。</summary>
        private static TextWord UnionWords(List<TextWord> line, int i0, int i1)
        {
            if (line == null || i0 < 0 || i1 >= line.Count || i0 > i1) return null;
            int minX = int.MaxValue, minY = int.MaxValue, maxR = int.MinValue, maxB = int.MinValue;
            StringBuilder sb = new StringBuilder();
            for (int i = i0; i <= i1; i++)
            {
                TextWord w = line[i];
                if (w.X < minX) minX = w.X;
                if (w.Y < minY) minY = w.Y;
                if (w.X + w.W > maxR) maxR = w.X + w.W;
                if (w.Y + w.H > maxB) maxB = w.Y + w.H;
                if (sb.Length > 0) sb.Append(' ');
                sb.Append(w.Text);
            }
            TextWord m = new TextWord();
            m.Text = sb.ToString();
            m.X = minX; m.Y = minY;
            m.W = maxR - minX; m.H = maxB - minY;
            return m;
        }

        /// <summary>
        /// 在单行内定位 needle：按“去空白 + 大小写归一”的紧凑文本查找，
        /// 命中后映射回覆盖的词区间并取并集包围盒——返回的是**目标文字自己的框**，
        /// 而不是整行中心。命中若跨越不同控件（间距过大）则跳过，
        /// 避免把两个相邻菜单项连成一个目标点。
        /// </summary>
        private static void LocateInLine(List<TextWord> line, string needle, bool exact,
                                         List<TextWord> matches)
        {
            if (line == null || line.Count == 0 || string.IsNullOrEmpty(needle)) return;

            StringBuilder compact = new StringBuilder();
            List<int> charWord = new List<int>();
            for (int wi = 0; wi < line.Count; wi++)
            {
                string t = Compact(line[wi].Text);
                for (int ci = 0; ci < t.Length; ci++)
                {
                    compact.Append(t[ci]);
                    charWord.Add(wi);
                }
            }
            string hay = compact.ToString();
            string nb = needle;
            if (!exact)
            {
                hay = hay.ToLowerInvariant();
                nb = nb.ToLowerInvariant();
            }
            if (nb.Length == 0 || hay.Length < nb.Length) return;

            int from = 0;
            while (from <= hay.Length - nb.Length)
            {
                int at = hay.IndexOf(nb, from, StringComparison.Ordinal);
                if (at < 0) break;
                int w0 = charWord[at];
                int w1 = charWord[at + nb.Length - 1];
                bool glued = true;
                for (int k = w0; k < w1; k++)
                {
                    if (!SameGroup(line[k], line[k + 1])) { glued = false; break; }
                }
                if (glued)
                {
                    TextWord m = UnionWords(line, w0, w1);
                    if (m != null) matches.Add(m);
                }
                from = at + nb.Length;
            }
        }

        /// <summary>
        /// 把一行按词间距切成语义块：间距超过阈值（SameGroup 判否）即切分。
        /// 每块是“一个可点控件”（菜单项、工具条按钮、表头单元格），块中心就是点击点。
        /// </summary>
        private static List<TextWord> SplitBlocks(List<TextWord> line)
        {
            List<TextWord> blocks = new List<TextWord>();
            if (line == null || line.Count == 0) return blocks;
            int start = 0;
            for (int i = 1; i < line.Count; i++)
            {
                if (!SameGroup(line[i - 1], line[i]))
                {
                    TextWord b = UnionWords(line, start, i - 1);
                    if (b != null) blocks.Add(b);
                    start = i;
                }
            }
            TextWord last = UnionWords(line, start, line.Count - 1);
            if (last != null) blocks.Add(last);
            return blocks;
        }
    }
}
