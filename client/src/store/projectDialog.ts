/** 项目弹窗开关（plan-20261009-ui U1）：侧栏「新建项目」、输入框工作区选择器「新建项目」、
 *  项目菜单「编辑项目」共用 App 挂载的同一个 ProjectDialog 实例。 */
import { create } from "zustand";

export interface ProjectDialogState {
  open: boolean;
  mode: "create" | "edit";
  projectId: number | null;
  show: (opts?: { mode?: "create" | "edit"; projectId?: number | null }) => void;
  hide: () => void;
}

export const useProjectDialogStore = create<ProjectDialogState>((set) => ({
  open: false,
  mode: "create",
  projectId: null,
  show: (opts) => set({ open: true, mode: opts?.mode ?? "create", projectId: opts?.projectId ?? null }),
  hide: () => set({ open: false }),
}));

export function openProjectDialog(opts?: { mode?: "create" | "edit"; projectId?: number | null }) {
  useProjectDialogStore.getState().show(opts);
}
