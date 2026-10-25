# 测试说明（chatcoder server）

## 怎么跑

在 `server/` 下执行（plan-562-2318 C2 更正：仓库根**没有** `scripts/` 目录，
所以文档里那种从根直接 `pwsh scripts/test.ps1` 的写法实际会 file-not-found）：

```powershell
pwsh scripts/test.ps1              # = fast，日常用这个
pwsh scripts/test.ps1 file grep    # 只跑文件名含 grep 的测试
pwsh scripts/test.ps1 changed      # 按 git 未提交改动推导相关测试
pwsh scripts/test.ps1 full         # 全量，带超时守护
pwsh scripts/test.ps1 fast -TimeoutSec 900   # 手动放宽超时
```

每个档位都有**硬超时**：超时会终止并打印最后 40 行输出，能看出卡在哪个用例，不会再无限挂死。
失败时脚本会单独摘出 `FAILED` / `ERROR` 清单，不用在长输出里翻。

| 档位 | 用例范围 | 实测耗时 | 用途 |
|---|---|---|---|
| `fast` | 排除 90 个实测 >8s 的文件 | **1085 passed / 7 分 34 秒** | 日常改动后自测 |
| `file` | 文件名匹配 | 单文件十秒~两分钟 | 定位排查、单点回归 |
| `changed` | 按 git 改动启发式匹配 | 视匹配数 | 提交前 |
| `full` | 全部 2100+ 用例 | 20 分钟以上 | 发版前 |

## 为什么测试慢（实测结论，不是猜测）

最初怀疑是逐用例重建表，实测后修正了判断：

1. **每个测试文件都要导入完整的 app 模块树**——这是真正的大头。实测 `import app.persistence`
   单独就要 **1.9s**，加上 `app.orchestration` / `app.gateway` / `app.services` 后，单个测试文件
   的固定开销在 8s 上下；`test_git_multirepo.py` 因为额外跑真实 git 操作要 **65s**。
   这是架构级开销，靠调整测试代码消不掉（需要拆包/惰性导入，属独立优化项）。
   即使排除了 90 个慢文件，`fast` 档剩下的 1085 个用例仍需 7 分半，其中大块来自
   `test_workflow_isolation.py` / `test_worktree_service.py` 这类真实跑 git worktree 的用例。
2. **真实等待**：子进程、内置浏览器、网络、超时类用例（`test_browser_kit_accuracy.py` 49s、
   `test_workflow_*`、`test_ta3_*`）。`fast` 档把它们整体排除。
3. **逐用例 drop_all + create_all**（已修）：原 `conftest.py` 每个用例重建 45 张表，
   实测 drop 0.20s + create 0.10s；2100 用例累计半小时以上，而且 DDL 与写引擎的短连接
   交错时会出现 `no such table` / `table ... already exists` 的随机失败。
   现在改为「幂等建表 + 同连接按外键逆序清数据」，单用例 setup 从 **1.06s 降到 0.01~0.08s**。
   注意：这一步只消除了固定开销中的一小半（每用例约 1s），**并未解决**上面第 1 条的导入开销。

## 隔离方式

- 测试强制使用临时库（`%TEMP%/chatcoder_pytest.db`），`app/persistence/database.py` 的
  `_force_test_database_if_needed()` 还会在进程内再加一道保护，防止误连真实库。
- `conftest.py` 的 `_reset_shared_test_db` 负责用例间隔离：幂等 `create_all` + 清空全表数据。
  保留 `create_all` 是刻意的——它是 `checkfirst` 语义，表齐全时只要 0.09s，却能兜住
  「上一个用例把表 drop 掉」的情况，明显减少假失败。
- 需要独立临时库的文件（如 `test_memory_scope.py`）会用**同名 fixture 覆盖**
  `_reset_shared_test_db`，并把写引擎、FTS 索引路径、工具侧连接工厂一起指向自己的 tmp 库。

## 注意

- 本库表**没有** `AUTOINCREMENT`（见 `persistence/database.py` 的说明），所以清数据后主键会
  从 1 重新开始；但也意味着**已删除的主键会被新数据复用**，涉及 id 复用的清理逻辑
  （`read_cache.clear_cache_for_session`、`post_edit_check.clear_fail_counts`）必须在会话
  永久删除时调用，否则会串会话。
- 并行跑多个 pytest 进程会共享同一个临时库文件、互相踩，不要并发跑本套测试。
