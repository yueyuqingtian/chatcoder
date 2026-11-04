import { useEffect, useRef, useState } from "react";
import { api, type SessionMentionCandidate, type TreeNode } from "../../api/client";

export type MentionCategory = "all" | "session" | "file";
export type MentionItem = { kind: "session"; session: SessionMentionCandidate } | { kind: "file"; path: string };

/** plan-20261104-session-mentions：独立请求状态与查询身份，迟到结果不能覆盖新会话。 */
export function useMentionCandidates(open: boolean, query: string, projectId: number | null, sessionId: number | null) {
  const [category, setCategory] = useState<MentionCategory>("all");
  const [allProjects, setAllProjects] = useState(false);
  const [archived, setArchived] = useState(false);
  const [retryFiles, setRetryFiles] = useState(0);
  const [retrySessions, setRetrySessions] = useState(0);
  const [fileTake, setFileTake] = useState(20);
  const [sessionPages, setSessionPages] = useState<{ key: string; items: SessionMentionCandidate[]; next: string | null }>({ key: "", items: [], next: null });
  const [sessionCursor, setSessionCursor] = useState<string | undefined>();
  const [files, setFiles] = useState<{ key: string; paths: string[] }>({ key: "", paths: [] });
  const [sessionLoading, setSessionLoading] = useState(false);
  const [fileLoading, setFileLoading] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [fileError, setFileError] = useState("");
  const identity = `${sessionId}:${projectId}`;
  const sessionKey = JSON.stringify([identity, query, allProjects, archived]);
  const fileKey = JSON.stringify([identity, query]);
  const sequence = useRef(0);
  const fileSequence = useRef(0);

  useEffect(() => { setCategory("all"); setAllProjects(false); setArchived(false); }, [identity]);
  useEffect(() => { setSessionCursor(undefined); }, [sessionKey]);
  useEffect(() => { setFileTake(20); }, [fileKey]);
  useEffect(() => {
    const request = ++sequence.current;
    if (!open || category === "file") return;
    // 查询变化后的首轮只使用空游标；上一轮的翻页游标不能附到新条件上。
    const cursor = sessionPages.key === sessionKey ? sessionCursor : undefined;
    setSessionLoading(true); setSessionError("");
    const timer = setTimeout(() => {
      void api.sessionMentionCandidates({ current_session_id: sessionId ?? undefined, project_id: projectId ?? undefined,
        all_projects: allProjects, query, include_archived: archived, cursor, limit: 20 })
        .then((page) => {
          if (request !== sequence.current) return;
          setSessionPages((prev) => ({ key: sessionKey,
            items: cursor && prev.key === sessionKey ? [...prev.items, ...page.items.filter((x) => !prev.items.some((p) => p.id === x.id))] : page.items,
            next: page.next_cursor }));
        }).catch(() => { if (request === sequence.current) setSessionError("会话加载失败"); })
        .finally(() => { if (request === sequence.current) setSessionLoading(false); });
    }, 250);
    return () => { clearTimeout(timer); ++sequence.current; };
    // sessionPages 只是保存结果，不应触发重复请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, category, sessionKey, sessionCursor, retrySessions]);

  useEffect(() => {
    const request = ++fileSequence.current;
    if (!open || category === "session") return;
    setFileError("");
    if (projectId == null) { setFiles({ key: fileKey, paths: [] }); setFileLoading(false); return; }
    setFileLoading(true);
    const timer = setTimeout(() => {
      const load = query.trim() ? api.projectFileSearch(projectId, query, fileTake + 1)
        : api.getProjectTree(projectId, 6).then((tree) => {
          const paths: string[] = [];
          const walk = (nodes: TreeNode[]) => { for (const n of nodes) {
            if (paths.length > fileTake) return;
            if (n.type === "file") paths.push(n.path.replace(/\\/g, "/"));
            if (n.children) walk(n.children);
          } };
          walk(tree.children || []); return paths;
        });
      void load.then((paths) => { if (request === fileSequence.current) setFiles({ key: fileKey, paths }); })
        .catch(() => { if (request === fileSequence.current) setFileError("文件加载失败"); })
        .finally(() => { if (request === fileSequence.current) setFileLoading(false); });
    }, 250);
    return () => { clearTimeout(timer); ++fileSequence.current; };
  }, [open, category, fileKey, fileTake, retryFiles, projectId, query]);

  const sessions = sessionPages.key === sessionKey ? sessionPages.items : [];
  const paths = files.key === fileKey ? files.paths : [];
  const nextSessionCursor = sessionPages.key === sessionKey ? sessionPages.next : null;
  const sessionItems: MentionItem[] = category === "file" ? [] : sessions.slice(0, category === "all" ? 6 : undefined).map((session) => ({ kind: "session", session }));
  const fileItems: MentionItem[] = category === "session" ? [] : paths.slice(0, category === "all" ? 6 : fileTake).map((path) => ({ kind: "file", path }));
  return { category, setCategory, allProjects, setAllProjects, archived, setArchived, sessionLoading, fileLoading,
    sessionError, fileError, sessionItems, fileItems, items: [...sessionItems, ...fileItems],
    moreSessions: category === "all" ? sessions.length > 6 || Boolean(nextSessionCursor) : Boolean(nextSessionCursor),
    moreFiles: paths.length > (category === "all" ? 6 : fileTake),
    loadSessions: () => category === "all" ? setCategory("session") : setSessionCursor(nextSessionCursor ?? undefined),
    loadFiles: () => category === "all" ? setCategory("file") : setFileTake((n) => n + 20),
    retrySessions: () => setRetrySessions((n) => n + 1), retryFiles: () => setRetryFiles((n) => n + 1) };
}
