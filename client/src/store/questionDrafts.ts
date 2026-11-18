import { create } from "zustand";
import type { AttachmentInfo } from "../api/client";

export type QuestionAnswer = string | string[];
export type QuestionAnswers = Record<string, QuestionAnswer>;
export type QuestionAttachments = Record<string, AttachmentInfo[]>;

interface QuestionDraft {
  step: number;
  answers: QuestionAnswers;
  custom: Record<string, string>;
  files: QuestionAttachments;
  uploading: number;
  submitting: boolean;
  error: string;
}

export const emptyQuestionDraft: QuestionDraft = {
  step: 0, answers: {}, custom: {}, files: {}, uploading: 0, submitting: false, error: "",
};

// plan-20261118-runtime-question-cache：审批编号全局唯一，草稿跟随提问，在主、侧会话间切换也能保留。
export const useQuestionDrafts = create<Record<string, QuestionDraft>>(() => ({}));
const timers = new Map<string, ReturnType<typeof setTimeout>>();

export function updateQuestionDraft(id: string, update: (draft: QuestionDraft) => Partial<QuestionDraft>) {
  useQuestionDrafts.setState((state) => {
    const draft = state[id] ?? emptyQuestionDraft;
    return { [id]: { ...draft, ...update(draft) } };
  });
}

export function clearQuestionDraft(id: string) {
  clearTimeout(timers.get(id));
  timers.delete(id);
  useQuestionDrafts.setState((state) => {
    if (!(id in state)) return state;
    const next = { ...state };
    delete next[id];
    return next;
  }, true);
}

export function startQuestionSubmission(id: string) {
  updateQuestionDraft(id, () => ({ submitting: true, error: "" }));
  clearTimeout(timers.get(id));
  timers.set(id, setTimeout(() => {
    rejectQuestionSubmission(id, "未收到答复回执，请检查连接后重试。");
  }, 8000));
}

export function rejectQuestionSubmission(id: string, error: string) {
  clearTimeout(timers.get(id));
  timers.delete(id);
  if (useQuestionDrafts.getState()[id]) {
    updateQuestionDraft(id, () => ({ submitting: false, error }));
  }
}

export function questionAttachmentRefs(files: QuestionAttachments) {
  return Object.fromEntries(Object.entries(files).filter(([, items]) => items.length > 0)
    .map(([index, items]) => [index, items.map(({ file_id, filename }) => ({ file_id, filename }))]));
}
