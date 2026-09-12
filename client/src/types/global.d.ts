export {};

declare global {
  interface Window {
    chatcoderAPI?: {
      selectDirectory: () => Promise<string | null>;
      selectFiles?: (filters?: Array<{ name: string; extensions: string[] }>, opts?: { allowDirectories?: boolean }) => Promise<string[]>;
      getBackendPort?: () => Promise<number>;
      openPath?: (path: string) => Promise<string>;
      showItemInFolder?: (path: string) => Promise<void>;
      openInApp?: (target: string, path: string) => Promise<boolean>;
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
      /** plan-546: 毛玻璃模式（Win11 acrylic；不支持时降级 CSS 半透明） */
      setGlassMode?: (on: boolean) => Promise<boolean>;
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
