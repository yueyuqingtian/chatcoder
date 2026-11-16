import type { InstalledPet, PetPref, PetsDirInfo } from "../pet/petApi";

export {};

/** 主进程 IPC 的统一失败形态（plan-693-2868 §6.5）：`main.cjs` 的 `communityFail()` 产出。
 *  `code` 供界面分支（NO_TOKEN / BAD_TOKEN / TIMEOUT …），`message` 已是中文可读文案。 */
declare global {
  /** 外置微应用的运行时状态（`microappsExt:status` / `microappsExt:list` 的条目）。
   *  与 electron/external-microapps.cjs 的 `statusOf()` 逐字段对应，改一处必须同步另一处。 */
  interface MicroappsExtStatus {
    id: string;
    name: string;
    version: string;
    description: string;
    /** 微应用目录绝对路径 */
    dir: string;
    /** microapp.json 的校验问题；非空时 valid=false，不允许拉起 */
    problems: string[];
    valid: boolean;
    hasServer: boolean;
    /** starting / running / exited / stopped / failed */
    state: string;
    port: number | null;
    exitCode: number | null;
    /** 已就绪时的前端地址（{port} 已回填）；未运行时为空串 */
    url: string;
    /** 最近若干行进程输出，崩溃排查用 */
    logTail: string[];
    /** 声明的方法面 */
    methods: string[];
  }

  interface MicroappsExtStartResult {
    ok: boolean;
    url?: string;
    port?: number | null;
    state?: string;
    reused?: boolean;
    dev?: boolean;
    external?: boolean;
    error?: string;
    problems?: string[];
    logTail?: string[];
  }

  interface MicroappsExtCallResult {
    ok: boolean;
    result?: unknown;
    truncated?: boolean;
    error?: string;
    message?: string;
    detail?: unknown;
  }

  interface CommunityBridgeFailure {
    ok: false;
    code: string;
    message: string;
  }

  /** 打开微应用独立窗口的入参（`microapp:openWindow`，见 electron/main.cjs 的 openMicroAppWindow）。
   *
   *  `kind` 决定窗口承载方式：
   *   · `builtin`：内置微应用（app:// 页面 + 宿主桥），窗口里加载 microapp.html 外壳再套 webview；
   *   · `external`：外置微应用（自带前后端），直接用 `url` loadURL。
   *  `uid` 是**窗口实例**的身份（主窗口渲染层派生为 `<标签uid>-w` 并绑定会话），
   *  与右侧面板里那个标签的 uid 不同——同 appId 允许同时存在面板标签与独立窗口。 */
  interface MicroAppWindowOpts {
    kind: "builtin" | "external";
    appId: string;
    version?: string;
    entry?: string;
    uid?: string;
    sessionId?: number | null;
    /** 外置微应用的前端地址（start 之后拿到的那个） */
    url?: string;
    /** 窗口标题里的应用名 */
    name?: string;
  }

  interface MicroAppWindowResult {
    ok: boolean;
    /** 窗口键（`builtin:<appId>` / `external:<appId>`）：关闭事件按它回传，用于释放绑定 */
    key?: string;
    /** true = 该 appId 已有窗口，本次只是聚焦（调用方不必再持有一份宿主桥绑定） */
    reused?: boolean;
    error?: string;
  }

  /** publish 入参：字段以 electron/community-github.cjs `publish()` 的 opts 为准（:816-825）。
   *  这里不复用 api/community.ts 的接口，避免 global.d.ts 反向依赖业务模块。 */
  interface CommunityPublishOpts {
    appId: string;
    version: string;
    /** 已打包好的 .ccapp / .zip 绝对路径（主进程校验扩展名与 100MB 上限） */
    filePath: string;
    manifest?: Record<string, unknown>;
    authorId?: string;
    authorName?: string;
    desc?: string;
    /** 成功后是否清理中转 Release（默认 true） */
    cleanup?: boolean;
    /** 社区仓库覆盖值（缺省用主进程记住的那份） */
    repo?: string;
  }

  /** withdraw 入参（community-github.cjs:968-971）。 */
  interface CommunityWithdrawOpts {
    appId: string;
    version: string;
    authorId?: string;
    authorName?: string;
    reason?: string;
    repo?: string;
  }

  /** 发布进度事件体（community:publish-progress，main.cjs:2489-2493）。 */
  interface CommunityProgressPayload {
    stage: string;
    percent?: number | null;
    sent?: number;
    total?: number;
    attempt?: number;
    maxAttempts?: number;
    issueUrl?: string;
    status?: string;
    message?: string;
    appId?: string;
    version?: string;
    action?: string;
  }
}

declare global {
  interface Window {
    chatcoderAPI?: {
      selectDirectory: () => Promise<string | null>;
      /** plan-20261009-ui U1：拖入的文件 / 文件夹 → 绝对路径（Electron 32+ 已移除 File.path，须经 webUtils；拿不到返回空串） */
      getPathForFile?: (file: File) => string;
      selectFiles?: (filters?: Array<{ name: string; extensions: string[] }>, opts?: { allowDirectories?: boolean }) => Promise<string[]>;
      getBackendPort?: () => Promise<number>;
      openPath?: (path: string) => Promise<string>;
      /** 用系统默认浏览器打开外部链接 */
      openExternal?: (url: string) => Promise<unknown>;
      /** 返回 ok=false 表示路径不存在等失败原因（前端据此提示，不再静默无反应） */
      showItemInFolder?: (path: string) => Promise<{ ok: boolean; error?: string; fallback?: string } | void>;
      openInApp?: (target: string, path: string) => Promise<{ ok: boolean; launcher?: string; error?: string }>;
      /** plan-308-1542 需求5：手动指定外部应用可执行文件（自动探测失败兜底） */
      selectApp?: (appName: string, currentPath?: string) => Promise<{ ok: boolean; path?: string; canceled?: boolean }>;
      /** plan-308-1542 需求5：查询已解析/已配置的启动器 */
      getExternalApps?: () => Promise<Record<string, { configured: string | null; resolved: string | null }>>;
      ptySpawn?: (opts: { cwd?: string; cols?: number; rows?: number; shell?: string }) => Promise<{ id: number; pid?: number; isPty?: boolean; error?: string }>;
      ptyWrite?: (id: number, data: string) => void;
      ptyResize?: (id: number, cols: number, rows: number) => void;
      ptyKill?: (id: number) => void;
      onPtyData?: (cb: (id: number, data: string) => void) => () => void;
      onPtyExit?: (cb: (id: number, code: number) => void) => () => void;
      onBrowserAnnotation?: (cb: (payload: unknown) => void) => () => void;
      openBrowserDevTools?: (webContentsId?: number) => Promise<boolean>;
      captureBrowserPage?: (webContentsId?: number) => Promise<string | null>;
      /** plan-20261015-browser-agent：面板 webview 交给主进程（关闭节流、挂 CDP、接管弹窗与新窗口） */
      browserAgentPrepare?: (webContentsId: number) => Promise<{ ok: boolean; debugger?: boolean; error?: string }>;
      /** 主进程侧浏览器操作：capture / network / netBody / aiWindow / events / dialogReply / clipboard */
      browserAgentExec?: (webContentsId: number, op: string, args?: Record<string, unknown>) => Promise<{ ok: boolean; result?: any; error?: string }>;
      /** 主进程事件：网页弹窗待用户处理（dialog / dialog-closed）、网页请求新窗口（popup） */
      onBrowserAgentEvent?: (cb: (payload: { type: string; wcId: number; [k: string]: unknown }) => void) => () => void;
      /* ── plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§6：微应用宿主桥的渲染层三通道 ──
         主进程把微应用页面的 window.microapp.invoke 转成 hostRequest 抛给渲染层，渲染层转发后端后
         用 hostReply 回执；下行流式日志用 hostPush 直达目标 guest（按 uid 定位）。
         实际使用者是 client/src/browserAgent/microappHost.ts（单例，勿在组件里重复订阅）。 */
      onMicroappHostRequest?: (cb: (payload: {
        uid?: string;
        appId?: string;
        version?: string;
        wcId?: number;
        reqId?: string;
        op?: string;
        args?: Record<string, unknown>;
      }) => void) => () => void;
      microappHostReply?: (payload: {
        reqId: string;
        uid?: string;
        ok: boolean;
        result?: unknown;
        reason?: string;
        message?: string;
      }) => void;
      microappHostPush?: (payload: {
        uid: string;
        wcId?: number;
        event: string;
        payload?: Record<string, unknown>;
      }) => Promise<{ ok: boolean; result?: unknown; error?: string }>;
      /* ── plan-external-microapps · docs/app1/13-external-microapps.md ──
         外置微应用宿主（主进程 electron/external-microapps.cjs）。外置微应用自带前后端与进程，
         不经宿主桥；渲染层只负责列清单 / 按需启动（点卡片打开界面）/ 停止进程 / 查状态 / 调方法。 */
      microappsExtList?: () => Promise<{ ok: boolean; apps?: MicroappsExtStatus[]; error?: string }>;
      microappsExtStart?: (id: string) => Promise<MicroappsExtStartResult>;
      microappsExtStop?: (id: string) => Promise<{ ok: boolean; already?: boolean; pid?: number; error?: string }>;
      microappsExtStatus?: (id: string) => Promise<{ ok: boolean; status?: MicroappsExtStatus | null; error?: string }>;
      microappsExtCall?: (id: string, method: string, args?: Record<string, unknown>) => Promise<MicroappsExtCallResult>;
      /* ── 微应用**独立窗口**（拓展页 / 已安装列表 / 右侧面板「在新窗口打开」）──
         主进程按 appId 去重：同一应用重复打开只聚焦已有窗口（reused=true），不会再开一个。
         `builtin` 走应用自己的 microapp.html 外壳（内含 app:// 的 webview，宿主桥照常工作），
         `external` 直接加载外置微应用自己的 http 地址（它有自己的后端，不经宿主桥）。 */
      openMicroAppWindow?: (opts: MicroAppWindowOpts) => Promise<MicroAppWindowResult>;
      /** 内置微应用窗口外壳的全屏按钮（外置微应用页面由主进程处理 F11 / Esc） */
      toggleMicroAppWindowFullscreen?: () => Promise<{ ok: boolean; fullscreen: boolean }>;
      onMicroAppWindowFullscreen?: (cb: (on: boolean) => void) => () => void;
      /** 某个微应用独立窗口被关掉：主窗口据此释放该窗口实例的宿主桥 uid 绑定 */
      onMicroAppWindowClosed?: (cb: (payload: { key?: string; appId?: string }) => void) => () => void;
      /** 某个微应用独立窗口加载失败（`did-fail-load`）：外置进程可能已退出，主窗口据此提示用户重开 */
      onMicroAppWindowLoadFailed?: (cb: (payload: {
        key?: string; appId?: string; name?: string;
        code?: number; desc?: string; url?: string;
      }) => void) => () => void;
      minimizeWindow?: () => void;
      toggleMaximize?: () => void;
      closeWindow?: () => void;
      /** plan-31-151 S2：主进程 maximize/unmaximize 事件通知。
       *  plan-20261016-ux：第二个参数为客户区真实外扩量（逻辑像素，正常为 0），供标题栏补偿 */
      onMaximizeChange?: (cb: (isMax: boolean, inset: { top: number; left: number; right: number } | null) => void) => () => void;
      /** plan-31-151 S3：自研拖拽通道已移除（标题栏改回系统 -webkit-app-region:drag） */
      /** plan-26-126 P1：一键重启（毛玻璃开关改为重启后生效） */
      relaunchApp?: () => Promise<{ ok: boolean; reason?: string }>;
      fixTextInput?: () => Promise<boolean>;
      /** plan-546/plan-308-1542: 毛玻璃模式（Win11 acrylic；Win10 ACCENT 系统模糊；mac vibrancy）
       *  返回 { ok, backend, reason? }；ok=false 时渲染层自动提高不透明度降级。 */
      setGlassMode?: (on: boolean) => Promise<{ ok: boolean; backend: string; reason?: string; verified?: number | null; needRestart?: boolean }>;
      /** 查询当前窗口实际玻璃状态（false =毛玻璃已关，标题栏/最大化走 Windows 原生行为） */
      getGlassActive?: () => Promise<boolean>;
      onRendererFocus?: (cb: () => void) => () => void;
      /** 主进程在窗口几何变化前同步通知，避免开头几帧仍按静止态重测。 */
      onWindowMotion?: (cb: (payload: { active?: boolean }) => void) => () => void;
      getUsername?: () => Promise<string>;
      setKeepAwake?: (on: boolean) => Promise<boolean>;
      /** 任务完成系统通知（仅桌面版）：mode 决定是否弹窗，主进程判定窗口聚焦状态；
       *  返回 true 表示已弹出（未弹/非桌面端为 false），点击通知回到 sessionId 对应会话。 */
      notifyTaskFinished?: (payload: {
        mode: "always" | "unfocused" | "off";
        title: string;
        body: string;
        sessionId?: number | null;
      }) => Promise<boolean>;
      /** 主题偏好同步（主进程落盘，下次启动 loading 页按此适配深浅色） */
      setThemePref?: (theme: "light" | "dark") => void;
      /** UI 偏好备份（plan-73-344）：localStorage 之外的第二落盘通道（启动时取较新者） */
      setUiPrefs?: (payload: { savedAt: number; prefs: object }) => void;
      getUiPrefs?: () => { savedAt?: number; prefs?: object } | null;
      /** 自动更新：检查 / 状态 / 下载 / 安装 / 版本（electron-updater） */
      checkForUpdates?: () => Promise<unknown>;
      getUpdateState?: () => Promise<unknown>;
      downloadUpdate?: () => Promise<unknown>;
      installUpdate?: () => Promise<boolean>;
      /** plan-20261022-update-route：取消下载（回到 available、断点保留）/ 切回第一优先级（镜像）重新开始 */
      cancelUpdateDownload?: () => Promise<unknown>;
      switchUpdateSourceToPrimary?: () => Promise<unknown>;
      /** plan-20261021-usage-update：更新设置（autoDownload = 发现新版本自动静默下载，默认开） */
      getUpdateSettings?: () => Promise<{ autoDownload: boolean }>;
      setUpdateSettings?: (patch: { autoDownload?: boolean }) => Promise<{ autoDownload: boolean }>;
      /** plan-20261024-update-channel：下载通道（测速结果 / 重新测速 / 手动指定 / 推送订阅）
       *  返回结构见 store/updater.ts 的 ChannelSnapshot，此处只用 unknown 透传，由 store 收窄。 */
      getUpdateChannels?: () => Promise<unknown>;
      measureUpdateChannels?: () => Promise<unknown>;
      setUpdateChannel?: (id: string) => Promise<unknown>;
      onUpdateChannels?: (cb: (snapshot: unknown) => void) => () => void;
      getAppVersion?: () => Promise<string>;
      onUpdateStatus?: (cb: (state: unknown) => void) => () => void;
      /** plan-230-1144 M4.2: 更新内容可见 —— 版本历史 / 本次更新汇总 / 首启消费标记 */
      getReleaseNotes?: (opts?: { limit?: number }) => Promise<{
        ok: boolean;
        source: "mirror" | "github" | "local" | "none";
        error?: string;
        releases: Array<{ version: string; name: string; date: string; notes: string; prerelease?: boolean }>;
      }>;
      getWhatsNew?: (opts?: { from?: string; to?: string }) => Promise<{
        ok: boolean;
        source: "mirror" | "github" | "none";
        error?: string;
        releases: Array<{ version: string; name: string; date: string; notes: string }>;
      }>;
      consumeWhatsNew?: () => Promise<{ show: boolean; version: string; from?: string }>;
      /** plan-73-323：宠物系统（设置页——偏好 / 图库 / 本地导入 / 来源链接） */
      petGetPref?: () => Promise<PetPref>;
      petSetPref?: (patch: Partial<PetPref>) => Promise<PetPref>;
      petListInstalled?: () => Promise<InstalledPet[]>;
      petListManifest?: (opts?: { force?: boolean; query?: string }) => Promise<{
        total: number;
        shown: number;
        pets: Array<{ slug: string; displayName: string; kind: string; author: string; previewUrl: string; spritesheetUrl: string; installed: boolean }>;
        fromCache: boolean;
        stale: boolean;
        error: string | null;
      }>;
      petInstall?: (slug: string) => Promise<InstalledPet>;
      petRemove?: (slug: string) => Promise<{ slug: string }>;
      petImportLocal?: () => Promise<{ ok: boolean; canceled?: boolean } & Partial<InstalledPet>>;
      petOpenPetPage?: (slug: string) => Promise<boolean>;
      petRevealPet?: (slug: string) => Promise<boolean>;
      /** 恢复显示（隐藏角标置 visible=false 后，从设置页恢复） */
      petShowPet?: () => Promise<boolean>;
      /** 宠物安装目录（设置 → 宠物）：dir 为当前生效目录；custom = 用户指定；fallback = 默认目录不可写已回落 */
      petGetDir?: () => Promise<PetsDirInfo>;
      /** 修改宠物安装目录（空串 = 恢复默认），已安装宠物自动迁移；返回迁移结果 */
      petSetDir?: (dir: string) => Promise<PetsDirInfo & { moved: number; skipped: number }>;
      petOpenDir?: () => Promise<PetsDirInfo>;
      /** 主进程偏好变更广播（宠物窗口与设置页共用同一份偏好真相） */
      onPetPrefChanged?: (cb: (pref: PetPref) => void) => () => void;
      /** 宠物面板「查看会话」：切到主窗对应会话 */
      onPetFocusSession?: (cb: (sessionId: number) => void) => () => void;
      /** 宠物面板齿轮：打开「设置 → 宠物」 */
      onPetOpenSettings?: (cb: () => void) => () => void;
      /** plan-693-2868（§6.5 / D20 / D36）：微应用社区发布与撤回。
       *
       *  只有桌面端有这条桥（`electron/preload.cjs:189-204` 暴露，主进程 `main.cjs` 里
       *  `ipcMain.handle("community:*")` 实现）。PAT 的加密存储、中转上传、issue 提交与轮询
       *  全在主进程，渲染层拿不到 token 原文。
       *  所有方法都返回 `{ok:true, ...}` 或 `{ok:false, code, message}`，**不抛异常**；
       *  有类型的封装见 `client/src/api/community.ts`（组件请走那里，不要直接用 window）。 */
      community?: {
        tokenStatus: () => Promise<{ ok: true; saved: boolean; encrypted: boolean } | CommunityBridgeFailure>;
        setToken: (token: string) => Promise<{ ok: true; saved: boolean; encrypted: boolean } | CommunityBridgeFailure>;
        clearToken: () => Promise<{ ok: true; saved: boolean; encrypted: boolean } | CommunityBridgeFailure>;
        getRepo: () => Promise<{ ok: true; repo: string } | CommunityBridgeFailure>;
        setRepo: (repo: string) => Promise<{ ok: true; repo: string } | CommunityBridgeFailure>;
        publish: (opts: CommunityPublishOpts) => Promise<{ ok: true } & Record<string, unknown> | CommunityBridgeFailure>;
        withdraw: (opts: CommunityWithdrawOpts) => Promise<{ ok: true } & Record<string, unknown> | CommunityBridgeFailure>;
        pollIssue: (issueNumber: number, opts?: Record<string, unknown>) => Promise<{ ok: true } & Record<string, unknown> | CommunityBridgeFailure>;
        /** 订阅发布进度事件；返回取消订阅函数 */
        onProgress: (cb: (payload: CommunityProgressPayload) => void) => () => void;
      };
    };
  }
}
