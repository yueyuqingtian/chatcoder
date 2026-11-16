/** 社区发布 / 撤回的渲染层薄封装（plan-693-2868 §6.5 / D20 / D32 / D34 / D36）。
 *
 *  为什么发布不走 HTTP：PAT 只在 Electron 主进程（`electron/community-github.cjs`），
 *  后端 Python 一条 PAT 也碰不到——「PAT 不出现在 Python 日志」这条验收正是靠这条边界成立的（D20）。
 *  所以这里只做一件事：把 `electron/preload.cjs` 已暴露的 `window.chatcoderAPI.community`
 *  包成有类型的函数，组件层不直接摸 `window`，也不重复写通道名。
 *
 *  通道名以 `electron/main.cjs` 的 `ipcMain.handle("community:*")` 为准（preload.cjs:189-204）：
 *  `community:token-status` / `community:set-token` / `community:clear-token` /
 *  `community:get-repo` / `community:set-repo` / `community:publish` / `community:withdraw` /
 *  `community:poll-issue`，进度事件 `community:publish-progress`。
 *
 *  主进程约定：所有方法都返回 `{ok:true, ...}` 或 `{ok:false, code, message}`，**不抛异常**
 *  （main.cjs:2499-2505 `communityFail`），所以这里也不 try/catch——把 code 原样交给界面做分支。
 */

/** 主进程统一的失败形态（`code` 用于界面给出可读提示，见 §7 P5 验收「PAT 失效有明确提示」）。 */
export interface CommunityFailure {
  ok: false;
  /** NO_TOKEN / BAD_TOKEN / TIMEOUT / NO_REPO / NETWORK / INVALID_APP_ID … */
  code: string;
  message: string;
}

/** PAT 状态：只回「有没有存、是否加密」，token 原文永不回渲染层。 */
export interface CommunityTokenStatus {
  ok: true;
  saved: boolean;
  encrypted: boolean;
}

/** 发布进度事件（`community:publish-progress`）：stage 为阶段名，percent 可为 null（等待期无进度）。 */
export type CommunityProgress = CommunityProgressPayload;

/** 发布结果：status 是 issue 的最终状态（D40），timedOut 表示「提交成功但没等到 Actions」。 */
export interface CommunityPublishResult {
  ok: true;
  /** published | rejected | unknown | pending（pending = 超时未完成） */
  status?: string;
  timedOut?: boolean;
  outcome?: string;
  /** Actions 在 issue 里的回执原文 */
  comment?: string;
  issueNumber?: number;
  issueUrl?: string;
  downloadUrl?: string;
  size?: number;
  sha256?: string;
  /** 超时不是失败：issue 已提交，Actions 可能还在排队（main.cjs:951-955） */
  code?: "TIMEOUT";
  message?: string;
  staging?: { repo: string; releaseId: number; tag: string; cleanup: { attempted: boolean; ok: boolean; warning: string } };
}

/** 撤回结果：只移除 apps.json 条目，Release 保留（D35）。 */
export interface CommunityWithdrawResult {
  ok: true;
  status?: string;
  timedOut?: boolean;
  outcome?: string;
  comment?: string;
  issueNumber?: number;
  issueUrl?: string;
  code?: "TIMEOUT";
  message?: string;
}

/** 发布入参：`filePath` 必须是已打包好的 `.ccapp` / `.zip` 绝对路径（主进程校验扩展名与体积）。 */
export interface CommunityPublishInput {
  appId: string;
  version: string;
  filePath: string;
  /** manifest 内容（主进程从中取 name/desc/perms/methods/vendors） */
  manifest?: Record<string, unknown>;
  /** 本机 `usr_` 标识（D15） */
  authorId?: string;
  /** 可选昵称（不可验证） */
  authorName?: string;
  /** 本次填的说明（覆盖 manifest 里的 desc） */
  desc?: string;
  /** 社区仓库 `owner/repo`。**必须显式传**：主进程也存了一份（`community:set-repo`），
   *  但设置页写的是 Python 侧的 `community_repo`——两处都可能是空/旧值，
   *  不传就会在「设置里明明填了仓库」的情况下报 NO_REPO。见 MicroAppsPanel 的取法。 */
  repo?: string;
}

type Bridge = NonNullable<Window["chatcoderAPI"]>["community"];

/** 取 IPC 桥；网页版（无 preload）返回 null —— 调用方据此隐藏发布入口而不是报错。 */
function bridge(): Bridge | null {
  if (typeof window === "undefined") return null;
  return window.chatcoderAPI?.community ?? null;
}

/** 桌面端是否支持发布/撤回（网页版没有 preload，主进程那套不可用）。 */
export function communityBridgeAvailable(): boolean {
  return bridge() !== null;
}

/** 统一失败补全：preload 缺失时给一个与主进程同形状的失败对象，界面不必区分两种来源。 */
function noBridge(): CommunityFailure {
  return {
    ok: false,
    code: "NO_BRIDGE",
    message: "当前环境不支持社区发布（桌面版才有）：网页调试时请用桌面端发布",
  };
}

export const communityBridge = {
  /** PAT 状态（首次发布前用它决定是否弹引导，D36）。 */
  tokenStatus: async (): Promise<CommunityTokenStatus | CommunityFailure> =>
    bridge()?.tokenStatus() ?? noBridge(),
  /** 保存 PAT（主进程 safeStorage 加密落盘；返回值不含 token 原文）。 */
  setToken: async (token: string): Promise<CommunityTokenStatus | CommunityFailure> =>
    bridge()?.setToken(token) ?? noBridge(),
  /** 清除本机 PAT。 */
  clearToken: async (): Promise<CommunityTokenStatus | CommunityFailure> =>
    bridge()?.clearToken() ?? noBridge(),
  /** 读主进程里记的社区仓库（owner/repo；空串 = 社区关闭）。 */
  getRepo: async (): Promise<{ ok: true; repo: string } | CommunityFailure> =>
    bridge()?.getRepo() ?? noBridge(),
  /** 写社区仓库（主进程侧那份，publish 默认用它）。 */
  setRepo: async (repo: string): Promise<{ ok: true; repo: string } | CommunityFailure> =>
    bridge()?.setRepo(repo) ?? noBridge(),
  /** 发布到社区：打包好的 `.ccapp` → 中转 Release → 提 [upload] issue → 轮询回执（§3.4）。 */
  publish: async (opts: CommunityPublishInput): Promise<CommunityPublishResult | CommunityFailure> =>
    bridge()?.publish(opts) ?? noBridge(),
  /** 撤回：提 [withdraw] issue → Actions 移除 apps.json 条目；包保留（D35）。 */
  withdraw: async (opts: {
    appId: string; version: string; authorId?: string; authorName?: string; reason?: string;
    /** 同 publish：显式带设置页里的 `community_repo`，不依赖主进程那份可能为空的缓存 */
    repo?: string;
  }): Promise<CommunityWithdrawResult | CommunityFailure> =>
    bridge()?.withdraw(opts) ?? noBridge(),
  /** 单独轮询某个 issue（重开对话框 / 点「刷新状态」时用）。 */
  pollIssue: async (issueNumber: number, opts?: Record<string, unknown>) =>
    bridge()?.pollIssue(issueNumber, opts) ?? noBridge(),
  /** 订阅发布进度事件；返回取消订阅函数（组件卸载时必须调用，否则监听越积越多）。 */
  onProgress: (cb: (p: CommunityProgress) => void): (() => void) =>
    bridge()?.onProgress(cb) ?? (() => { /* 无桥时给一个空取消函数，调用方无需分支 */ }),
};

/** 失败码 → 可读提示（§7 P5 验收：「PAT 失效有明确提示」）。
 *
 *  三个主线索（任务书点名的）单列，其余落到通用兜底但保留主进程给的 message——
 *  主进程的 message 已经是中文可读的（`community-github.cjs` 所有 throw 点都带 hint）。
 */
export function communityErrorText(code: string, message?: string): string {
  switch (code) {
    case "NO_TOKEN":
      return "还没有保存 GitHub Personal Access Token：请先填写（勾选 public_repo 即可）";
    case "BAD_TOKEN":
    case "INVALID_TOKEN":
    case "EMPTY_TOKEN":
    case "TOKEN_UNREADABLE":
      return "GitHub Token 无效或已过期（需要 public_repo 权限）：请重新填写";
    case "TIMEOUT":
      // 超时不是失败：issue 已经提上去了（D40），只是 Actions 还没跑完
      return "已提交但 5 分钟内未等到 Actions 完成，可稍后打开 issue 链接查看结果";
    case "NO_REPO":
      return "未配置社区仓库（形如 owner/repo）：请先在「设置 → 拓展 → 微应用」里填写";
    case "NO_BRIDGE":
      return "网页版不支持发布：请用桌面端操作";
    case "NETWORK":
      return "无法连接 GitHub：请检查网络或代理后重试";
    default:
      return message || `发布失败（${code}）`;
  }
}
