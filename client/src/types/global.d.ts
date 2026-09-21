export {};

declare global {
  interface Window {
    chatcoderAPI?: {
      selectDirectory: () => Promise<string | null>;
      selectFiles?: (filters?: Array<{ name: string; extensions: string[] }>, opts?: { allowDirectories?: boolean }) => Promise<string[]>;
      getBackendPort?: () => Promise<number>;
      openPath?: (path: string) => Promise<string>;
      showItemInFolder?: (path: string) => Promise<void>;
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
      minimizeWindow?: () => void;
      toggleMaximize?: () => void;
      closeWindow?: () => void;
      fixTextInput?: () => Promise<boolean>;
      /** plan-546/plan-308-1542: 毛玻璃模式（Win11 acrylic；Win10 ACCENT 系统模糊；mac vibrancy）
       *  返回 { ok, backend, reason? }——失败时前端可明确提示降级原因。 */
      setGlassMode?: (on: boolean) => Promise<{ ok: boolean; backend: string; reason?: string; verified?: number | null }>;
      /** plan-308-1542 需求4：模糊能力探测（无系统后端时设置页提示降级） */
      glassCapability?: () => Promise<{ backend: string; supported: boolean; reason?: string }>;
      /** plan-308-1555 M0：毛玻璃诊断（DWM 回读 + 渲染层 alpha 链路采样，把"看不到"变成可判定结论） */
      glassDiagnostics?: () => Promise<Record<string, unknown>>;
      /** plan-308-1555 M5：玻璃自检模式（临时调淡面板 alpha，肉眼一眼判定桌面是否混入） */
      setGlassSelfCheck?: (on: boolean) => Promise<{ ok: boolean; on: boolean }>;
      onRendererFocus?: (cb: () => void) => () => void;
      getUsername?: () => Promise<string>;
      setKeepAwake?: (on: boolean) => Promise<boolean>;
      /** 主题偏好同步（主进程落盘，下次启动 loading 页按此适配深浅色） */
      setThemePref?: (theme: "light" | "dark") => void;
      /** 自动更新：检查 / 状态 / 下载 / 安装 / 版本（electron-updater） */
      checkForUpdates?: () => Promise<unknown>;
      getUpdateState?: () => Promise<unknown>;
      downloadUpdate?: () => Promise<unknown>;
      installUpdate?: () => Promise<boolean>;
      getAppVersion?: () => Promise<string>;
      onUpdateStatus?: (cb: (state: unknown) => void) => () => void;
      /** plan-230-1144 M4.2: 更新内容可见 —— 版本历史 / 本次更新汇总 / 首启消费标记 */
      getReleaseNotes?: (opts?: { limit?: number }) => Promise<{
        ok: boolean;
        source: "github" | "local" | "none";
        error?: string;
        releases: Array<{ version: string; name: string; date: string; notes: string; prerelease?: boolean }>;
      }>;
      getWhatsNew?: (opts?: { from?: string; to?: string }) => Promise<{
        ok: boolean;
        source: "github" | "none";
        error?: string;
        releases: Array<{ version: string; name: string; date: string; notes: string }>;
      }>;
      consumeWhatsNew?: () => Promise<{ show: boolean; version: string; from?: string }>;
    };
  }
}
