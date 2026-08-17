/** ComposerBox（v19 插件化）：对话态输入框薄封装——核心逻辑统一在 ComposerCore。 */
import { ComposerCore } from "./ComposerCore";

export function ComposerBox() {
  return <ComposerCore variant="chat" />;
}
