export {};

declare global {
  interface Window {
    chatcoderAPI?: {
      selectDirectory: () => Promise<string | null>;
      openPath?: (path: string) => Promise<string>;
      showItemInFolder?: (path: string) => Promise<void>;
      ptySpawn?: (opts: { cwd?: string }) => Promise<{ id: number; error?: string }>;
      ptyWrite?: (id: number, data: string) => void;
      ptyKill?: (id: number) => void;
      onPtyData?: (cb: (id: number, data: string) => void) => () => void;
      onPtyExit?: (cb: (id: number, code: number) => void) => () => void;
      onBrowserAnnotation?: (cb: (payload: unknown) => void) => () => void;
      minimizeWindow?: () => void;
      toggleMaximize?: () => void;
      closeWindow?: () => void;
    };
  }
}
