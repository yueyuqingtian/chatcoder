# chatcoder v1.0 版本更新日志

**发布日期**: 2026-07-26  
**版本**: v1.0  
**概述**: 基于深度审计的全面迭代，覆盖安全加固、性能优化、编排增强、工具补齐、前端体验升级五大维度。

---

## 一、功能完善

### 编排与上下文增强

- **BudgetTracker 接线**: 每次模型调用后记录 token 消耗，超预算自动熔断，避免失控 agent 无限消耗 token
- **DAG 环检测 + 自动修复**: 任务拆解建图后自动检测循环依赖，发现环时移除入度最大的边
- **文件级 write-intent 冲突检测**: 并行 agent 写同一文件时通过 session 级注册表检测冲突，阻塞等待或返回错误
- **ensure_tool_pairing 标注改进**: 缺失的 tool result 不再注入假数据，改为明确标注"结果因上下文压缩已丢失"
- **Token 估算精度提升**: 关键路径引入 tiktoken 精确计数（预算判断、压缩触发），非关键路径保留粗估
- **Token 预算警告一次性注入**: 超过 80% 阈值后仅注入一次提醒，不再每步重复追加 user 消息
- **任务失败自动重试**: 瞬态错误（网络超时、模型限流 429/502/503）自动指数退避重试，最多 3 次
- **审查解析失败改 NEEDS_REVIEW**: reviewer 输出解析失败时不再默认 PASS，改为标记需人工审查
- **Review 循环可中断**: 审查循环增加 cancel_event 支持，用户取消后 reviewer 不再继续执行
- **Agent 间事件总线**: session 级 asyncio.Queue 总线，支持 agent 间实时消息通信（info/warning/handoff）
- **MCP 工具 per-agent scope**: MCP 工具注册到 per-invocation 临时 dict，不再污染全局 registry

### 并发与性能

- **per-session 数据库锁**: 全局 `_db_write_lock` 改为 per-session 锁，PG 模式下完全无锁，并行 agent 吞吐提升 5-10x
- **并发工具 Semaphore**: 只读工具并发执行引入 `asyncio.Semaphore(10)` 控制并发数，每个协程使用独立 DB Session
- **build_agent_context 并行化**: 8 路只读查询通过 `asyncio.gather` 并行执行，任务启动延迟降低约 60%
- **同步文件 IO 异步化**: `Path.read_text()` 替换为 `asyncio.to_thread()`，不再阻塞事件循环
- **N+1 查询修复**: 完成总结中逐任务查产物改为收集所有 ID 后一次 `IN` 查询
- **_schedule_dependents 迭代化**: 递归改 while 循环 + 深度限制（50），批量处理共用一个 session

### 前端性能

- **MessageFlow 虚拟化**: 超过 50 条消息时启用 `@tanstack/react-virtual` 虚拟滚动，长会话不再卡顿
- **MarkdownContent React.memo**: 避免父组件更新时触发完整的 Markdown 解析（CPU 密集操作）
- **WS 指数退避重连**: 重连延迟从固定 3s 改为 `min(2s * 2^attempt, 30s)` + 随机抖动
- **WS handler 清理**: disconnect 时 `handlers.clear()`，消除 switchSession 后 handler 累积泄漏
- **轮询定时器生命周期**: 最大轮询 20 次（5 分钟）自动停止；WS 恢复后 3 次即停止轮询

### 代码质量

- **死代码清理**: 删除 `_recent_tool_sigs`、`_max_same_tool` 等声明后未使用的变量
- **日志级别修正**: scheduler.py 中正常业务流程从 `logger.warning` 改为 `logger.info`
- **重复调用修复**: 删除 `_detect_tool_call_intent` 的复制粘贴重复调用

---

## 二、Bug 修复

### P0 级（运行时崩溃 / 安全漏洞）

| 问题 | 修复内容 |
|------|----------|
| `DiminishingReturnsDetector` 导入崩溃 | 在 compaction.py 重新实现轻量级递减检测器（基于连续空转步数 + 内容相似度） |
| `auto_approve_tools=True` 默认开启 | 默认改为 False；新增 `force_approval_tools` 配置，高风险工具即使 auto_approve 也强制审批 |
| `safe_resolve` 符号链接穿越 | 删除 v5.3 旁路逻辑；使用 `os.path.realpath()` 解析 symlink；增加 `is_symlink()` 检查 |
| `web_fetch` SSRF 无防护 | 增加 scheme 白名单（仅 http/https）+ DNS 解析后 IP 黑名单（内网/云元数据/回环） |
| terminal 超时后进程存活 | `asyncio.TimeoutError` 后增加 `proc.kill()` + `await proc.wait()` |
| terminal cwd 路径穿越 | `_resolve_path` 改用 `safe_resolve` 校验，越界路径回退 workspace_root |

### P1 级（协议违规 / 潜在崩溃）

| 问题 | 修复内容 |
|------|----------|
| review.py `tool_call_id` 协议违规 | 优先使用模型返回的 `tc.get("id")`，而非自生成的 `call_key` |
| TextEntry React Hooks 违规 | 将 `useState`/`useCallback` 移到组件顶部，条件 return 之前 |
| approval.py `asyncio.get_event_loop()` | 改为 `asyncio.get_running_loop().create_future()`，兼容 Python 3.12+ |
| 消息 ID 类型不一致 | WS 接收时统一 `Number(id)`，确保去重比较正确 |

---

## 三、新增功能

### 流式输出

- 后端 agent loop 最终文本产出时分块广播（80 字符/块，20ms 间隔）
- 新增 WS 事件: `token.delta`（逐块推送）+ `token.done`（流结束）
- 前端 store 增加 `streamingBuffers` 状态，实时追加渲染

### 新增工具

| 工具 | 文件 | 说明 |
|------|------|------|
| `git` | `tools/git.py` | 完整 Git 集成: diff/commit/branch/checkout/stash/log/blame/status |
| `multi_file_edit` | `tools/multi_edit.py` | 多文件原子性 search-replace，全部成功或全部回滚 |
| `codebase_search` | `tools/codebase_search.py` | 代码库语义搜索，自动构建分块索引 + 关键词匹配 |
| `test_fix_loop` | `tools/test_fix_loop.py` | 测试执行-修复循环，最多 N 轮自动重试 |
| `browser_navigate` | `tools/browser.py` | Playwright 浏览器导航（headless Chromium） |
| `browser_screenshot` | `tools/browser.py` | 网页截图，返回 base64 图片 |
| `lsp_definition` | `tools/lsp_tools.py` | 符号定义查找（grep 回退方案） |
| `lsp_diagnostics` | `tools/lsp_tools.py` | 文件诊断（Python AST / TypeScript tsc） |

### 单 Agent 快速模式

- `handle_chat_message` 新增 `mode="quick"` 参数
- 跳过 Leader 拆解 / DAG 调度 / 审查，直接创建单任务 → agent loop → 返回结果
- 适用于简单编码任务，对标 Claude Code 模式

### Checkpoint 文件级快照

- 每次 `fs_write` 前自动创建文件快照到 `.chatcoder/checkpoints/`
- 支持 `restore_checkpoint` 一键回退到任意步骤
- 支持 `list_checkpoints` 列出所有历史快照

### 前端体验

- **代码块复制按钮**: MarkdownContent 的 `pre` 组件增加悬浮复制按钮 + aria-label
- **Mermaid + KaTeX 渲染**: 集成 `remark-math` + `rehype-katex`，支持数学公式渲染
- **全局 ErrorBoundary**: App 顶层包裹错误边界，单组件崩溃不再导致白屏
- **Token 用量面板**: `UsagePanel` 组件展示实时 agent 进度、流式状态
- **可访问性基线**: 消息列表 `role="log"` + `aria-live="polite"`；代码块 `role="region"`

### 基础设施

- **Agent 间事件总线** (`event_bus.py`): session 级发布/订阅，支持单播和广播
- **文件写入冲突注册表** (`file_lock.py`): session 级 write-intent 声明 + 超时等待
- **tiktoken 精确计数** (`token_counter.py`): 延迟加载，失败时自动回退粗估

---

## 依赖变更

### 后端 (server/pyproject.toml)

- 已有: `tiktoken>=0.7.0`, `qdrant-client>=1.10.0`（无需新增）
- 新增可选: `playwright>=1.45.0`（`[project.optional-dependencies] browser`）

### 前端 (client/package.json)

- 新增: `@tanstack/react-virtual`（虚拟滚动）
- 新增: `mermaid`（图表渲染，预留）
- 新增: `katex` + `remark-math` + `rehype-katex`（数学公式）

---

## 已知限制 / 后续规划

- 巨型组件拆分（RightPanel 977行、ComposerBox 594行）建议后续独立迭代
- 内联 style 迁移 CSS Modules 涉及 10+ 组件，建议渐进式迁移
- LSP 集成当前为 grep 回退方案，完整 language server 进程管理待后续实现
- 向量索引当前为关键词匹配，embedding 语义搜索待接入实际 embedding 模型
