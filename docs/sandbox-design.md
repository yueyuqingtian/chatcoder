# 沙箱设计（Sandbox Design）

> v3.0 (plan-88) 立项。当前实现状态：**P0 已落地**（配置生效 + 审批门决策），
> P1/P2 为路线图。本文件是方案原文，实现以代码为准。

## 1. 背景与现状

项目已有 `SandboxMode` 三态枚举（`core/enums.py`）与 config_service 默认值
（`sandbox_mode: workspace-write`），但**全仓库没有任何编排层消费它**：
`terminal_exec` 直接 `create_subprocess_shell`，无进程隔离、无文件系统边界，
仅靠 shell_policy 黑名单 + 审批门兜底。

`native/@deepseek-ai/node-addon-landlock-run` 是 Node 侧 landlock 绑定，
Python 服务端不可直接复用，需另行选型。

## 2. 威胁模型

假设对手 = 被模型诱导执行的恶意/失误命令（prompt injection、路径错误、
`rm -rf`、网络下载执行等）。资产 = 用户本机数据、凭据、工作区外文件。

| 威胁 | 现状防护 | 缺口 |
|---|---|---|
| 删除/篡改工作区外文件 | 无 | 命令黑名单只挡固定模式 |
| 读取敏感文件（~/.ssh、.env 等） | 无 | cwd 限制可被 `type C:\...` 绕过 |
| 恶意命令执行（下载→运行） | shell_policy deny + 审批门 | 审批门可被「始终允许」长期跳过 |
| 资源耗尽（fork 炸弹、死循环） | 超时 kill（仅本进程） | 不杀进程树 |
| 横向渗透（读取环境变量/凭据） | 无 | 子进程继承全部环境变量 |

## 3. 三级沙箱语义

`SandboxMode`（`read-only` / `workspace-write` / `danger-full-access`）：

| 模式 | 写工作区 | 读工作区 | 访问外部 | 审批门 |
|---|---|---|---|---|
| read-only | 禁止 | 允许 | 禁止（命令层面） | 非只读命令直接拒绝 |
| workspace-write（默认） | 允许 | 允许 | 禁止（路径穿越防护） | 保留（低风险免审） |
| danger-full-access | 允许 | 允许 | 允许 | 跳过（显式 deny 仍拦截） |

决策优先级（高 → 低）：
1. shell_policy deny（命令黑名单，`terminal.run` 内强制，任何模式不可绕过）
2. 沙箱模式硬边界（read-only 拒绝写盘/高危命令；danger-full-access 免审批）
3. exec_policy 显式规则（deny > allow）
4. 会话权限模式（plan/readonly 写盘拒绝、accept_edits 写盘免审）
5. 工具自身 approval_precheck（terminal 只读命令免审）

## 4. 平台实现选型

### 4.1 Windows：Job Object（P1）

- `CreateJobObjectW` + `SetInformationJobObject`（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）：
  服务端崩溃/超时 kill 时整棵进程树随之终止，解决当前"只杀 shell 不杀子进程"问题。
- 资源限制：`JOBOBJECT_BASIC_LIMIT_INFORMATION` 的内存上限（`JOB_OBJECT_LIMIT_PROCESS_MEMORY`）
  与活动进程数上限（`JOB_OBJECT_LIMIT_ACTIVE_PROCESS`），防 fork 炸弹。
- 可选受限令牌（`CreateRestrictedToken`）：去掉 SeDebugPrivilege 等敏感权限。
- 依赖：ctypes 直调 kernel32（无第三方依赖）或 pywin32（可选）。

### 4.2 Unix：landlock + prctl（P1/P2）

- **P2 文件系统边界**：landlock LSM（Linux ≥ 5.13）为子进程建立 `allowed_access` 文件
  系统 ACL——`read-only` 模式只放行 workspace 读；`workspace-write` 放行 workspace 读写，
  其余目录一律 EACCES。macOS 无 landlock，退化为仅保留审批门。
- **P1 进程树 kill**：`prctl(PR_SET_PDEATHSIG, SIGKILL)` 父进程死亡自动杀子，
  配合超时 kill 全树。

### 4.3 降级语义

平台不支持（如 macOS 无 landlock、Windows 上 landlock 不适用）时**静默降级为
「无隔离但保留审批门」**，并在诊断面板/系统提示中标注当前沙箱实际能力，不假装隔离。

### 4.4 环境变量净化（P1）

子进程默认剥离高风险凭据（`*_API_KEY`、`*_TOKEN`、`AWS_*` 等）环境变量，
工作区模式除外（模型调试需要时经显式配置放行）。

## 5. 分阶段路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 配置生效：`ToolContext.sandbox_mode` 全链路透传；executor 审批门消费三级语义 | ✅ 已落地（本任务） |
| P1 | 进程隔离：Windows Job Object / Unix PDEATHSIG + 资源上限；环境变量净化 | 路线图 |
| P2 | 文件系统写边界：landlock ACL / 受限令牌；`writable_paths` 白名单落地 | 路线图 |

## 6. P0 落地细节（本任务）

- `ToolContext.sandbox_mode: str = "workspace-write"`（`tools/base.py`）。
- `agent_loop.run_agent_loop` 每 turn 计算一次：
  `effective_config(db, project_path=workspace, project_id=...)['sandbox_mode']`
  注入所有 ToolContext（主代理、子代理、拆分执行共用同一入口）。
- `executor._precheck_approval`：
  - `read-only`：写盘工具（fs_write/editor_apply_diff/multi_file_edit）直接拒绝，
    terminal_exec 非 shell_policy `allow` 命令直接拒绝，拒绝理由含「只读沙箱」。
  - `danger-full-access`：落在 exec_policy / 权限模式判定之后、审批卡之前——
    免审批执行（shell_policy deny 仍在 `terminal.run` 内拦截）。
  - `workspace-write`：现状不变。

## 7. 已知边界与后续

- read-only 下 `ask_user_question` 等交互工具仍走原审批/交互流程（不属写盘/命令）。
- 沙箱是纵深防御的一层，不替代审批门与规则系统；P1/P2 落地时本文件同步更新。
