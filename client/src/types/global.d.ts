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
      /** plan-26-126 P6：自研标题栏拖拽（原生 drag 区会吞掉双击，改由渲染层接管）
       *  start → move（逐帧）→ end 三步上报；主进程按屏幕坐标 setPosition。 */
      startWindowDrag?: () => void;
      moveWindowDrag?: () => void;
      endWindowDrag?: () => void;
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
