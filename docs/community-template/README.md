# chatcoder-microapps · 社区微应用仓库

这是 ChatCoder 微应用的**共享社区仓库**。所有人从同一个地址读到同一份 `apps.json`，微应用包统一托管在本仓库的 Release 里。

## 仓库主人只需做一次

1. 在 GitHub 新建一个**公开**仓库，名字建议 `chatcoder-microapps`（与 ChatCoder 主仓库分开，纯数据，不含程序代码）。
2. 把本目录三个文件放进仓库根目录：
   - `apps.json`
   - `README.md`
   - `.github/workflows/accept.yml`
3. 进仓库 **Settings → Actions → General**，确认 Workflow permissions 选 **Read and write permissions**。
4. 完事。**不需要给任何人加协作者**，也不需要审批谁。

之后每一次「上传/撤回」都由 `.github/workflows/accept.yml` 自动处理：校验格式与哈希、把包搬到本仓库 Release、改 `apps.json`、回执并关闭 issue。

## 用户在 ChatCoder 里怎么发布

1. 在微应用里点「发布到社区」，首次会让你填一个 GitHub Personal Access Token（勾 `public_repo` 即可）。
2. 软件会自动：打包 → 算 `sha256` → 在**你自己的仓库**建一个临时 Release 上传包（中转用）→ 到本仓库提一个 `[upload]` issue。
3. 等几十秒，issue 被自动处理并关闭，条目就出现在 `apps.json` 里了。

撤回同理：点「撤回」会提一个 `[withdraw]` issue，处理完只把条目从 `apps.json` 移除，**Release 里的包保留**，避免已下载过的链接失效。

## apps.json 里一条长什么样

```json
{
  "appId": "pipeline-canvas",
  "name": "管线画布",
  "version": "1.0.0",
  "authorId": "usr_7f3c1a2b",
  "authorName": "可选昵称",
  "desc": "把管线协议画成时序图",
  "size": 1048576,
  "sha256": "…64 位十六进制…",
  "perms": ["read_workspace"],
  "methods": ["drawTimeline", "highlight"],
  "vendors": [],
  "downloadUrl": "https://github.com/<owner>/chatcoder-microapps/releases/download/pipeline-canvas-1.0.0/pipeline-canvas-1.0.0.ccapp",
  "createdAt": "2026-01-01T00:00:00Z",
  "updatedAt": "2026-01-01T00:00:00Z"
}
```

## 几条约定

- `appId` 必须匹配 `^[a-z0-9][a-z0-9._-]{1,62}$`，发布后不要改名。
- 版本号用语义化 `MAJOR.MINOR.PATCH`；同 `appId` + 同版本不覆盖，要改就升版本。
- 包上限 100MB；仓库本身只放 `apps.json`，**不要把包提交进仓库**（GitHub 单文件硬限 100MB，且会把仓库撑大）。
- 作者标识由客户端本机生成（`usr_…`），**不可验证**；看到可疑条目可以用 ChatCoder 的拉黑功能按作者或条目屏蔽。
- 这里没有审核。所有内容由发布者自负其责。

## 读取地址

```
https://raw.githubusercontent.com/<owner>/chatcoder-microapps/HEAD/apps.json
```

ChatCoder 会自动带 `?t=<时间戳>` 击穿 CDN 缓存（GitHub 的 raw 有 5 分钟缓存，刚发布的条目最长 5 分钟后可见）。
