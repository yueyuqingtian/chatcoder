/** 设置中心：AI 规则（v2.2 对齐 zcode 3.18）。
 * 全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用。
 * plan-234-1171 R7: 项目规则按「具体项目」存储——顶部可选项目并显式展示「项目名 · 路径」。
 *
 * plan-114-450：
 *  - 来源开关「修改即保存」：此前只改本地 state、必须点「保存配置」才生效，用户以为关了其实没关
 *    （「无法关闭某个规则来源」的直接原因；后端另修了子目录文件不受控、全部停用回落为全部启用）；
 *  - 全局 / 项目规则文本 300ms 防抖自动保存，去掉「保存配置」按钮（AGENTS.md：设置页修改即保存）；
 *  - 排版改为统一 Section：页头右上「重新扫描」，分组为 项目 / 规则来源 / 全局规则 / 项目规则。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconFileText } from "../icons";
import { Button, CardRow, Empty, List, ListRow, PageActions, Section, Select, Tag, Textarea, Tooltip } from "../ui";
import { Sw } from "./shared";

type Source = { source: string; label: string; enabled: boolean };
type Scanned = { source: string; label: string; path: string; exists: boolean; kind: string };

/** 300ms 防抖：同一字段连续输入只提交最后一次；卸载时立即提交未发出的修改 */
function useDebouncedSave(save: (v: string) => Promise<void>) {
  const timer = useRef<number | null>(null);
  const pending = useRef<string | null>(null);
  const saveRef = useRef(save);
  saveRef.current = save;
  const flush = useCallback(() => {
    if (timer.current) { window.clearTimeout(timer.current); timer.current = null; }
    if (pending.current != null) { const v = pending.current; pending.current = null; void saveRef.current(v); }
  }, []);
  useEffect(() => flush, [flush]);
  return useCallback((v: string) => {
    pending.current = v;
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(flush, 300);
  }, [flush]);
}

export function RulesPanel() {
  const [sources, setSources] = useState<Source[]>([]);
  const [globalRules, setGlobalRules] = useState("");
  const [workdirRules, setWorkdirRules] = useState("");
  const [scanned, setScanned] = useState<Scanned[]>([]);
  const [scanning, setScanning] = useState(false);
  // 当前配置归属的项目路径（决定项目规则的存取键）
  const [projectPath, setProjectPath] = useState<string>("");

  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const activeProjects = projects.filter((p) => !p.archived);

  useEffect(() => {
    if (projectPath) return;
    const cur = activeProjects.find((p) => p.id === currentProjectId) ?? activeProjects[0];
    if (cur) setProjectPath(cur.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectId, projects]);

  const load = useCallback(async (path: string) => {
    try {
      const cfg = await api.getAiRules(path || undefined);
      setSources(cfg.sources);
      setGlobalRules(cfg.global_rules || "");
      setWorkdirRules(cfg.workdir_rules || "");
      if (cfg.project_path && !path) setProjectPath(cfg.project_path);
    } catch { /* 忽略：面板保持上次内容 */ }
    try { setScanned(await api.scanAiRules(path || undefined)); } catch { /* 忽略 */ }
  }, []);

  useEffect(() => { if (projectPath) void load(projectPath); }, [projectPath, load]);

  const fail = (what: string, e: unknown) => useChatStore.setState({ error: `${what}失败: ${String(e)}` });

  const handleScan = async () => {
    setScanning(true);
    try { setScanned(await api.scanAiRules(projectPath || undefined)); }
    catch (e) { fail("扫描", e); }
    finally { setScanning(false); }
  };

  /** 乐观更新 + 立即保存；失败回滚并提示 */
  const toggleSource = async (src: string, on: boolean) => {
    const prev = sources;
    const next = sources.map((s) => (s.source === src ? { ...s, enabled: on } : s));
    setSources(next);
    try {
      const cfg = await api.setAiRules({ enabled_sources: next.filter((s) => s.enabled).map((s) => s.source), project_path: projectPath || undefined });
      setSources(cfg.sources);
    } catch (e) { setSources(prev); fail("更新规则来源", e); }
  };

  const saveGlobal = useDebouncedSave(async (v) => {
    try { await api.setAiRules({ global_rules: v, project_path: projectPath || undefined }); } catch (e) { fail("保存全局规则", e); }
  });
  const saveWorkdir = useDebouncedSave(async (v) => {
    try { await api.setAiRules({ workdir_rules: v, project_path: projectPath || undefined }); } catch (e) { fail("保存项目规则", e); }
  });

  const selectedProject = activeProjects.find((p) => p.path === projectPath);
  const projectName = selectedProject ? (selectedProject.name || selectedProject.path) : "";
  const filesOf = (src: string) => scanned.filter((x) => x.source === src);
  // 有文件的来源排前面：用户关心的是「项目里实际存在的规则」
  const ordered = [...sources].sort((a, b) => Number(filesOf(b.source).length > 0) - Number(filesOf(a.source).length > 0));
  const foundCount = sources.filter((s) => filesOf(s.source).length > 0).length;

  return (
    <>
      <PageActions>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} loading={scanning} onClick={handleScan}>重新扫描</Button>
      </PageActions>

      <Section title="项目">
        <CardRow title="规则归属项目" desc={<Tooltip content={projectPath}><span className="sb-mono">{projectPath || "未选择项目"}</span></Tooltip>}>
          <Select
            className="rules-project-select"
            value={selectedProject?.id != null ? String(selectedProject.id) : ""}
            onChange={(v) => {
              const picked = activeProjects.find((p) => String(p.id) === v);
              if (picked) setProjectPath(picked.path);
            }}
            options={activeProjects.length === 0
              ? [{ value: "", label: "暂无项目" }]
              : activeProjects.map((p) => ({ value: String(p.id), label: p.name || p.path }))}
            aria-label="规则归属项目"
          />
        </CardRow>
      </Section>

      <Section title={`规则来源 · 项目里发现 ${foundCount} 个`} desc="关闭后，该来源的规则文档（含子目录中的同名文件）不再注入 Agent 上下文，立即生效" bare>
        {sources.length === 0 ? (
          <div className="ui-card"><Empty title="暂无规则来源" mood="think" /></div>
        ) : (
          <List>
            {ordered.map((s) => {
              const files = filesOf(s.source);
              return (
                <ListRow
                  key={s.source}
                  muted={!s.enabled}
                  icon={<IconFileText size={16} />}
                  name={s.label}
                  meta={files.length > 0
                    ? files.map((x) => <Tooltip key={x.path} content={x.path}><span><Tag mono>{x.path}</Tag></span></Tooltip>)
                    : <span className="ui-list-row-desc" style={{ marginTop: 0 }}>当前项目未发现该来源的规则文档</span>}
                  actions={<Sw checked={s.enabled} onChange={(v) => void toggleSource(s.source, v)} />}
                />
              );
            })}
          </List>
        )}
      </Section>

      <Section title="全局规则" desc="对所有项目生效，输入后自动保存" bare>
        <Textarea
          className="sb-code-editor"
          rows={8}
          style={{ minHeight: 160 }}
          placeholder="例如：回复使用简体中文；提交前运行类型检查…"
          value={globalRules}
          onChange={(e) => { setGlobalRules(e.target.value); saveGlobal(e.target.value); }}
          aria-label="全局规则"
        />
      </Section>

      <Section title={projectName ? `项目规则 · ${projectName}` : "项目规则"} desc="只对上面选中的项目生效，输入后自动保存" bare>
        <Textarea
          className="sb-code-editor"
          rows={8}
          style={{ minHeight: 160 }}
          placeholder="当前项目的约定，例如目录结构、命名规范…"
          value={workdirRules}
          onChange={(e) => { setWorkdirRules(e.target.value); saveWorkdir(e.target.value); }}
          aria-label="项目规则"
        />
      </Section>
    </>
  );
}
