# 发布流程

推送 tag 即发布：GitHub Actions 在 Windows 机器上完整打包、自检、发布到 GitHub Releases，
自建更新源由服务器定时直接从 GitHub 拉取，本机全程不需要打包环境、不需要服务器凭据。

```
提交版本号与更新内容 → git push origin v<版本>
        │
        ▼
GitHub Actions（.github/workflows/release.yml，windows-latest）
  build-release.ps1 -CI：内置 Chromium / 桌面操控内核 → 前端（含设计规范守卫）→ PyInstaller 后端
                       → 更新说明（CHANGELOG 当前版本区块）→ electron-builder → 安装包自检
  发布：先建草稿、上传三件套（安装包 / blockmap / latest.yml），再公开
        │
        ▼
自建源服务器（/opt/chatcoder-updates/sync-updates.sh，systemd 定时器每 5 分钟）
  比对 GitHub 最新正式版的 latest.yml → 有新版本：下载 → sha512 校验 → 原子部署 → 刷新更新历史 → 清理旧版本
        │
        ▼
客户端：镜像 → 自建源（快速通道）→ GitHub，三条线路拿到的都是同一份安装包与更新说明；
镜像线路走本机直连、只用一个连接（gh-proxy 对多连接并发会降速）
```

## 发一个新版本

1. 把 `package.json` 的 `version` 改成新版本号，`CHANGELOG.md` 写好对应的 `## v<版本>` 区块（规范见 CLAUDE.md 第 3 节），提交并推送。
2. 打 tag 并推送：

   ```bash
   git tag v0.9.1
   git push origin v0.9.1
   ```

3. 在仓库的 Actions 页查看「发布安装包」，约 30～40 分钟完成。tag 与 `package.json` 版本不一致、`CHANGELOG.md` 缺少该版本区块时会在第一步直接失败，不会产出半成品。
4. 发布完成后最多 5 分钟，自建源会自动拉到新版本；客户端每 20 分钟检查一次更新。

版本号带 `-`（如 `v1.0.0-beta.1`）时会发布为预发布版：GitHub 上可见，但不会成为「最新正式版」，自建源与客户端都不会推送它。

## 手动重跑 / 只打包不发布

Actions →「发布安装包」→ Run workflow：

- 填已有的 tag：按该 tag 重新打包，并覆盖同名 Release 的资产与说明；
- 不填 tag：只打包当前分支，三件套作为本次运行的 Artifacts 保留 7 天，不发布。

## 仓库 Secret（可选）

| 名称 | 作用 |
|---|---|
| `SERVER_ENV` | 后端默认配置（本地 `server/.env` 的全文），打包时写入安装包的 `resources/server/chatcoder-server/.env`。不配置时安装包不带默认模型，用户在设置里添加自己的模型即可。注意：Release 是公开下载的，写进去的内容所有人都能拿到。 |

## 自建更新源（一次性安装）

服务器上装好定时同步后，发版不再需要任何本机操作：

```bash
python tools/sync-updates.py --install
```

它会把 `tools/server-sync-updates.sh` 装到服务器的 `/opt/chatcoder-updates/sync-updates.sh`，并启用 systemd 定时器
`chatcoder-updates.timer`（没有 systemd 时写 cron）。每次只取 GitHub 上约 1KB 的 `latest.yml` 比对版本，没有新版本就立即退出。

常用命令（服务器上）：

```bash
systemctl list-timers chatcoder-updates.timer        # 下次执行时间
journalctl -u chatcoder-updates -n 50                # 最近的同步日志
/opt/chatcoder-updates/sync-updates.sh               # 立即同步一次
```

不想等定时器时，也可以在本机立即触发一次（顺带把服务器上的脚本更新为仓库最新版）：

```bash
python tools/sync-updates.py --version 0.9.1
```

额外的国内下载镜像（对象存储 / CDN）通过环境变量 `EXTRA_MIRRORS` 写进 `latest.yml` 的 `mirrors`，客户端无需发版即可使用，
见 `tools/server-sync-updates.sh` 文件头说明。

## 本地打包（兜底）

GitHub Actions 不可用时，本地仍可完整打包或发布，流程与 CI 完全一致：

```powershell
powershell -ExecutionPolicy Bypass -File build-release.ps1            # 只打包
powershell -ExecutionPolicy Bypass -File build-release.ps1 -Publish   # 打包并发布（需 gh auth login）
```

产物目录取 `package.json` 的 `build.directories.output`。后端打包依赖锁定在 `server/requirements-build.txt`，
本地升级 Python 依赖后需要按文件头的说明重新生成，CI 才会用上同样的版本。

## 发布前的自动检查

| 检查 | 位置 | 拦住什么 |
|---|---|---|
| tag 与版本号一致、CHANGELOG 有本版本区块 | 工作流第一步 | 发错版本、没有更新说明 |
| 设计规范守卫（`npm run lint:design`） | 前端构建 | 新增非令牌圆角、字面量过渡时长、左侧装饰色条、原生 title |
| 内置 Chromium / 桌面操控内核 / 后端产物齐全 | build-release.ps1 | 浏览器工具、电脑操控在用户端不可用 |
| 安装包自检（`tools/verify-package.cjs`） | electron-builder 之后 | 主进程模块漏打包（启动即报 Cannot find module）、资源缺失 |
| latest.yml 版本号与更新说明 | electron-builder 之后 | 客户端检查不到新版本、看不到「更新了什么」 |
