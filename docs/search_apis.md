# 免费搜索引擎接入调研（v31.2）

目标：为 web_search 工具接入多个免费搜索源，并支持「设置里配置代理后自动走代理」。

## 一、候选引擎与实测连通性（2026-08-28，本机网络环境）

| 引擎 | 端点 | API Key | 直连可达 | 备注 |
|------|------|---------|---------|------|
| Bing | `www.bing.com/search?q=` | 无 | ✅ 200 | 已接入（v31.1 噪音治理后可用） |
| Ecosia | `www.ecosia.org/search?q=` | 无 | ✅ 200 | 基于 Bing，免费无 key，可直连 |
| Google | `www.google.com/search?q=` | 无（Custom Search API 要 key） | ❌ 000 | 需代理；无头抓取有反爬风险 |
| DuckDuckGo | `html.duckduckgo.com/html/?q=` | 无 | ❌ 000 | 需代理；经典免费 HTML 端点 |
| Mojeek | `www.mojeek.com/search?q=` | 无 | ⚠️ 403 | 直连被拒，走代理可再测 |
| Brave | `search.brave.com/search?q=` | 无 | ❌ 000 | 需代理 |
| Startpage | `www.startpage.com/sp/search` | 无 | ❌ 000 | 需代理 |
| SearXNG | 自建/公共实例 | 无 | ❌ 000 | 元搜索，公共实例不稳定 |

> 实测方法：curl 与 httpx 双测（两者网络路径不同：curl 可达 cn.bing.com 而 httpx 不可达，反之 www.bing.com 两者可达）。
> 结论：**当前无代理直连仅 Bing/Ecosia 可用；Google/DuckDuckGo 等必须走代理**，
> 与需求「设置代理后搜索引擎自动走代理」完全吻合。

## 二、接入方案

### 1. 统一 http 客户端（代理支持）
- 新增 `server/app/core/http_client.py`：`get_proxy_url()` + `build_http_client()`。
- 代理来源优先级：运行时 `settings.http_proxy` > 环境变量 `HTTP_PROXY/HTTPS_PROXY`。
- 显式传 `proxy=` 给 `httpx.AsyncClient`（不依赖 trust_env，更可控）。
- 修复 `load_persisted_workspace()` 启动时不恢复 http_proxy 的 gap。

### 2. 多引擎适配器（统一协议）
- `server/app/orchestration/tools/search_base.py`：`SearchAdapter` 协议 + 通用工具
  （`real_url` 解包 / `html.unescape` / 相关性过滤 `filter_relevant`）。
- `ecosia.py` / `google.py` / `duckduckgo.py`：各自实现 `search(query, max_results)`。

### 3. web_search 工具改造
- schema 增加 `engine` 参数：`bing`（默认）/ `ecosia` / `google` / `duckduckgo`。
- 调度到对应适配器；解析结果统一走过滤与截断。
- 默认 `bing` 保持现状；`google`/`duckduckgo` 在无代理时会明确报错提示配置代理。

## 三、解析规则（各引擎页面结构）

| 引擎 | 结果块选择器 | 标题 | 摘要 |
|------|-------------|------|------|
| Bing | `li.b_algo` | `h2 a` | 首个 `p` |
| Google | `div.g`（含 `a[href^="http"]`） | `h3` | 摘要 div/span |
| DuckDuckGo | `div.result`（`a.result__a`） | `a.result__a` | `a.result__snippet` |
| Ecosia | `div.result`（`a.result__a`） | `a.result__a` | `p.result__snippet` |

## 四、风险与边界
- Google 无头抓取：数据中心 IP 即使走代理也可能弹验证码，需在工具描述中提示。
- 各引擎页面结构变更会破坏解析（与 Bing 同风险），解析函数需兼容多种 class 变体。
- 代理配置只影响搜索引擎/web 工具的网络请求，不影响 LLM 网关请求。
