import type { InstalledPet, PetPref } from "../pet/petApi";

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
      /** plan-31-151 S2：主进程 maximize/unmaximize 事件通知（修复最大化 8px 溢出裁切） */
      onMaximizeChange?: (cb: (isMax: boolean) => void) => () => void;
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
      /** 主进程偏好变更广播（宠物窗口与设置页共用同一份偏好真相） */
      onPetPrefChanged?: (cb: (pref: PetPref) => void) => () => void;
      /** 宠物面板「查看会话」：切到主窗对应会话 */
      onPetFocusSession?: (cb: (sessionId: number) => void) => () => void;
      /** 宠物面板齿轮：打开「设置 → 宠物」 */
      onPetOpenSettings?: (cb: () => void) => () => void;
    };
  }
}
