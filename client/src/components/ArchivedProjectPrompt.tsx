/** plan-278-1391：归档项目提示弹窗（全局单例）。
 *
 * 背景：projects.path 为唯一列，同路径再次创建时直接 INSERT 会撞唯一约束（500）。
 * 后端改为：同路径且已归档 → 返回 409 + detail{code:"project_archived", ...}；
 * 前端 store 把它落到 archivedProjectPrompt，这里统一弹出「恢复并打开」确认框——
 * 因此侧栏「新建项目」、命令中心「打开工作区」、输入框「添加目录」三处入口
 * 都能得到一致体验，无需各自实现弹窗。
 */
import { useChatStore } from "../store/chat";
import { useI18n } from "../store/i18n";
import { ConfirmDialog } from "./ConfirmDialog";

export function ArchivedProjectPrompt() {
  const prompt = useChatStore((s) => s.archivedProjectPrompt);
  const restore = useChatStore((s) => s.restoreArchivedProject);
  const dismiss = useChatStore((s) => s.dismissArchivedProjectPrompt);
  const { t } = useI18n();

  if (!prompt) return null;
  const label = prompt.name || prompt.path;

  return (
    <ConfirmDialog
      open
      title={t("prompt.project_archived_title")}
      message={t("prompt.project_archived_msg", { name: label })}
      confirmLabel={t("prompt.project_archived_restore")}
      cancelLabel={t("common.cancel")}
      onConfirm={() => void restore(prompt.projectId)}
      onCancel={dismiss}
    />
  );
}
