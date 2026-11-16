/** 拓展页第四标签「微应用」（plan-693-2868 D1 / §7 P4-2、P5）。
 *
 *  为什么单独成文件而不是塞进 MarketPanel：市场三类（插件 / 技能 / 连接器）的数据源是
 *  `/api/market/*`，微应用走 `/api/microapps` + `/api/community`（D11「不扩 MarketKind」）——
 *  两套接口、两套动作（注入 / 查看界面 / 发布 / 撤回），混在一个组件里只会让两边都难读。
 *  MarketPanel 只负责标签切换，内容在这里。
 *
 *  三条产品约束：
 *  ① 「注入到输入框」只注入引用、**不打开右侧面板**（与 P1 的 / 菜单同语义，§7 P1）；
 *     chip 的构造复用 ComposerCore 里那份（跨组件用 `chatcoder:composer-microapp` 事件传达）。
 *  ② 发布 / 撤回走 Electron IPC（`api/community.ts`），PAT 不出主进程（D20）；
 *     首次发布先弹 PAT 引导（D36），不在设置页静默等待。
 *  ③ 撤回只移除索引条目、包保留（D35）——确认文案必须说清这一点，否则用户不敢点。
 */
import {
  useCallback, useEffect, useMemo, useRef, useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  api,
  type CommunityAppOut, type CommunityListOut, type MicroAppOut,
} from "../../api/client";
import {
  communityBridge, communityBridgeAvailable, communityErrorText,
  type CommunityProgress,
} from "../../api/community";
import { usePanelStore, bucketSessionId } from "../../store/panel";
import { useMicroAppsStore } from "../../store/microapps";
import { describeExtError, useMicroappsExtStore } from "../../store/microappsExt";
import { notify } from "../../store/toast";
import { injectMicroAppRef } from "../../utils/microappInject";
import { openMicroAppWindow } from "../../utils/microappWindow";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
import { Badge, Button, Empty, Input, Menu, Tooltip } from "../ui";
import {
  IconArrowUpRight, IconBox, IconDownload, IconExternalLink, IconMoreHorizontal, IconPuzzle, IconTrash,
} from "../icons";

/** 一行 = 一个（appId, version）：本机已装的排前面，社区独有（未装）的排后面。 */
interface MicroAppRow {
  key: string;
  appId: string;
  name: string;
  version: string;
  description: string;
  perms: string[];
  sizeBytes: number;
  /** 本机是否已装该 appId（装了就能注入 / 查看界面） */
  installed: boolean;
  /** 社区索引里是否有同名同版本（有才能撤回——没发布过的条目撤不回来） */
  published: boolean;
  /** 作者标识（社区条目有；本地手写的没有，D15 不可验证） */
  author?: string;
  /** 来源：本机库的 `.origin.json`，社区独有条目固定 community */
  source: "local" | "import" | "community";
  /** plan-external-microapps：外置微应用（自带前后端与进程）——在网格里与普通微应用同款展示，
   *  唯一差别是点卡片=打开界面（它的进程由宿主按需拉起） */
  external?: boolean;
}

/** 体积展示：库管理与社区条目都用这一份，避免两处口径不同（100MB 上限是硬约束） */
function formatBytes(n?: number | null): string {
  const v = Number(n) || 0;
  if (v <= 0) return "—";
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(1)} MB`;
}

/** 发布进度阶段 → 中文（主进程的 stage 名见 community-github.cjs） */
const STAGE_TEXT: Record<string, string> = {
  hashing: "正在计算包哈希…",
  auth: "正在校验 GitHub Token…",
  "staging-repo": "正在准备中转仓库…",
  release: "正在创建中转 Release…",
  uploading: "正在上传包…",
  issue: "正在提交发布 issue…",
  waiting: "等待 Actions 处理（最长 5 分钟）…",
  done: "已完成",
  error: "失败",
};

/** 外链一律交给主进程打开（网页版没有 preload 桥，静默忽略；不在这里另做一套降级）。 */
function openExternal(url: string) {
  const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
  void w.chatcoderAPI?.openExternal?.(url);
}

/** 来源徽标（plan-693-2868 D15/D16）：与卡片其它徽标同一档 `xs`，颜色只区分「来自社区 / 来自本机」。 */
const SOURCE_BADGE: Record<MicroAppRow["source"], { label: string; tone: "neutral" | "accent" | "info" }> = {
  local: { label: "本地", tone: "neutral" },
  import: { label: "本地导入", tone: "accent" },
  community: { label: "社区", tone: "info" },
};

export function MicroAppsPanel({ query, reloadToken = 0 }: { query: string; reloadToken?: number }) {
  const [local, setLocal] = useState<MicroAppOut[]>([]);
  const [community, setCommunity] = useState<CommunityListOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  /** 社区读失败的提示（社区不可用不该影响本机库的展示，所以单独一条而不是整页错误） */
  const [communityError, setCommunityError] = useState<string | null>(null);

  // ── 发布 / 撤回 ───────────────────────────────────────────
  const [publishTarget, setPublishTarget] = useState<MicroAppRow | null>(null);
  const [tokenSaved, setTokenSaved] = useState<boolean | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [pkgPath, setPkgPath] = useState("");
  const [publishDesc, setPublishDesc] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [progress, setProgress] = useState<CommunityProgress | null>(null);
  const [publishResult, setPublishResult] = useState<{ ok: boolean; text: string; url?: string } | null>(null);
  const [withdrawTarget, setWithdrawTarget] = useState<MicroAppRow | null>(null);
  const [withdrawing, setWithdrawing] = useState(false);
  /** 桌面上才有发布能力（网页版没有 preload 桥） */
  const hasBridge = communityBridgeAvailable();
  /** plan-external-microapps：外置微应用清单（主进程扫 ~/.chatcoder/microapps-ext）。
   *  它和本机库是两套来源：本机库走 `/api/microapps`，外置的带自己的进程。但**展示上不做区分**——
   *  用户要求它就是一个普通微应用：同一张卡片网格、同样只显示名称/说明/版本/来源，
   *  点卡片即打开界面（进程由宿主按需拉起）。 */
  const extItems = useMicroappsExtStore((s) => s.items);
  const extAvailable = useMicroappsExtStore((s) => s.available);
  const extError = useMicroappsExtStore((s) => s.error);
  /** 正在拉起进程的那一行（只用于防重复点击；卡片上没有操作按钮） */
  const [extBusy, setExtBusy] = useState<string | null>(null);
  /** 进度订阅的取消函数：弹窗关闭 / 卸载时必须退订，否则监听越积越多 */
  const unsubRef = useRef<(() => void) | null>(null);

  const loadExt = useCallback(async () => {
    const store = useMicroappsExtStore.getState();
    if (!store.available) return;
    await store.load(true);
  }, []);

  /** 外置微应用：点卡片 = 打开界面。先让主进程把进程拉起来拿到真实地址，再开独立窗口。 */
  const openExtInterface = async (row: MicroAppRow) => {
    if (extBusy === row.key) return;
    const store = useMicroappsExtStore.getState();
    const item = store.resolve(row.appId);
    if (item && !item.valid) {
      notify.error(`微应用声明不完整：${item.problems.join("；")}`);
      return;
    }
    if (item?.state === "running" && item.url) {
      await openMicroAppWindow({ appId: row.appId, version: row.version, externalUrl: item.url, name: row.name });
      return;
    }
    setExtBusy(row.key);
    try {
      const res = await store.start(row.appId);
      if (!res.ok || !res.url) {
        notify.error(describeExtError(String(res.error || "")));
        return;
      }
      await openMicroAppWindow({ appId: row.appId, version: row.version, externalUrl: res.url, name: row.name });
    } finally {
      setExtBusy(null);
    }
  };

  const load = useCallback(async (opts?: { refresh?: boolean }) => {
    setLoading(true);
    try {
      const [localRes, communityRes] = await Promise.allSettled([
        api.listMicroApps(),
        api.listCommunityApps({ refresh: opts?.refresh }),
        // plan-external-microapps：外置清单与上面两路并行取，互不阻塞——
        // 社区断网不该让外置条目也显示不出来
        loadExt(),
      ]);
      if (localRes.status === "fulfilled") setLocal(localRes.value?.apps ?? []);
      else setLocal([]);
      if (communityRes.status === "fulfilled") {
        setCommunity(communityRes.value);
        setCommunityError(null);
      } else {
        // 社区读不到（未配置 / 断网）时保留上一次的列表，只提示原因：
        // 本机库照常可用，不因为社区挂了就整页空白
        setCommunityError(String(communityRes.reason));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  /** 顶部刷新按钮 / 导入成功后由父组件递重载信号（微应用不走市场分页，没有自己的刷新入口）。
   *  只在信号真的变化时重拉：初始 0 不触发（首帧已由上面的 load 拉过一次）。 */
  useEffect(() => {
    if (reloadToken > 0) void load({ refresh: true });
  }, [reloadToken, load]);
  useEffect(() => () => { unsubRef.current?.(); }, []);

  /** 合并本机 + 社区为一张表：同一 appId 只出一行（已装优先），社区独有的标「可安装」。 */
  const rows = useMemo<MicroAppRow[]>(() => {
    const out: MicroAppRow[] = [];
    const communityApps: CommunityAppOut[] = community?.apps ?? [];
    for (const m of local) {
      const hit = communityApps.find((c) => c.appId === m.appId && c.version === m.version);
      out.push({
        key: `local:${m.appId}@${m.version}`,
        appId: m.appId,
        name: m.name || m.appId,
        version: m.version,
        description: m.description || "",
        perms: m.perms ?? [],
        sizeBytes: m.sizeBytes ?? 0,
        installed: true,
        published: Boolean(hit),
        author: (m.origin?.authorName as string) || (m.origin?.authorId as string) || undefined,
        source: (m.origin?.source as MicroAppRow["source"]) || "local",
      });
    }
    // 外置微应用与普通微应用同一张卡片网格、同一套字段（用户要求：别给它单独一套样式）。
    // 它没有「安装/移除/发布」语义，所以卡片里只有名称、说明、版本与来源，**不渲染任何操作按钮**；
    // 点卡片本体即打开界面（进程由宿主按需拉起，见 openExtInterface）。
    for (const x of extItems) {
      out.push({
        key: `ext:${x.id}`,
        appId: x.id,
        name: x.name || x.id,
        version: x.version || "1.0.0",
        // 声明不完整时把问题当说明显示：这是用户唯一能看到的线索，不能藏起来
        description: x.valid ? x.description || "" : x.problems.join("；"),
        perms: [],
        sizeBytes: 0,
        installed: true,
        published: false,
        source: "local",
        external: true,
      });
    }
    const installedKeys = new Set(local.map((m) => `${m.appId}@${m.version}`));
    for (const c of communityApps) {
      if (installedKeys.has(`${c.appId}@${c.version}`)) continue;
      out.push({
        key: `community:${c.appId}@${c.version}`,
        appId: c.appId,
        name: c.name || c.appId,
        version: c.version,
        description: c.desc || "",
        perms: c.perms ?? [],
        sizeBytes: Number(c.size) || 0,
        installed: false,
        published: true,
        author: c.authorName || c.authorId,
        source: "community",
      });
    }
    return out;
  }, [local, community, extItems]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((r) =>
      `${r.name} ${r.appId} ${r.description}`.toLowerCase().includes(q));
  }, [rows, query]);

  /** 注入到输入框：写草稿 + 通知已挂载的输入框（两条通道见 utils/microappInject），
   *  **不打开面板**——注入 ≠ 打开界面（§7 P1 验收）。 */
  const inject = (row: MicroAppRow) => {
    injectMicroAppRef({ appId: row.appId, label: row.name, version: row.version });
    notify.success(`已注入「${row.name}」：回到对话页即可看到引用（随消息发送给 AI）`);
  };

  /** 查看界面：用户显式点击才打开（§7 P1 验收「注入不弹窗」）。
   *
   *  为什么要开**独立窗口**而不是右侧面板标签：设置页不属于任何会话，面板按会话分桶，
   *  开进去用户根本看不到（表现为「点了没反应」）。独立窗口是应用级窗口，与会话解耦；
   *  会话 id 仍按「用户最后停留的会话桶」带上，AI 侧的工作区定位不因此丢失。 */
  const openInterface = (row: MicroAppRow) => {
    void openMicroAppWindow({
      appId: row.appId,
      version: row.version,
      sessionId: bucketSessionId(usePanelStore.getState().activeBucket),
      name: row.name,
    });
  };

  /** 社区安装：装完默认不启用 → 重新拉一次本机库把它变成「已安装」行。 */
  const installFromCommunity = async (row: MicroAppRow) => {
    setBusy(row.key);
    try {
      await api.installCommunityApp(row.appId, row.version);
      notify.success(`已安装「${row.name}」${row.version}：默认不打开界面，需要时点「查看界面」`);
      await load();
      // plan-693-2868 回归修复：装完必须强制刷新 `/` 菜单与工具卡共用的那份清单
      // （store/microapps.ts），否则新应用要重启应用才出现在菜单里。
      // 与 InstalledPanel 的删除路径对称（那边删除后也要 load(true)）。
      await useMicroAppsStore.getState().load(true);
    } catch (e) {
      notify.error(`安装失败：${String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  /** 社区仓库（`owner/repo`）：发布 / 撤回时**显式**传给主进程。
   *
   *  为什么要显式传：主进程也存了一份仓库地址（`community:set-repo`），但设置页写的是
   *  Python 侧的 `community_repo`，两份可能不同步——只靠主进程那份会出现
   *  「设置里明明填了仓库，点发布却报 NO_REPO」。这里取社区列表返回的那份（就是服务端
   *  实际在用的），取不到再退回设置项。 */
  const resolveRepo = useCallback(async (): Promise<string | undefined> => {
    const fromList = community?.repo?.trim();
    if (fromList) return fromList;
    try {
      const s = await api.getGlobalSettings();
      return s.community_repo?.trim() || undefined;
    } catch {
      return undefined;
    }
  }, [community?.repo]);

  const openPublish = async (row: MicroAppRow) => {
    setPublishTarget(row);
    setPkgPath("");
    setPublishDesc(row.description);
    setPublishResult(null);
    setProgress(null);
    setTokenInput("");
    // D36：先看有没有存过 PAT —— 没有就地把引导页放在同一弹窗的第一步，不另开一个
    const st = await communityBridge.tokenStatus();
    setTokenSaved(st.ok ? st.saved : null);
  };

  const closePublish = () => {
    unsubRef.current?.();
    unsubRef.current = null;
    setPublishTarget(null);
    setPublishing(false);
  };

  const saveToken = async () => {
    const st = await communityBridge.setToken(tokenInput.trim());
    if (!st.ok) {
      notify.error(communityErrorText(st.code, st.message));
      return;
    }
    setTokenSaved(st.saved);
    setTokenInput("");
    notify.success(
      st.encrypted
        ? "GitHub Token 已加密保存"
        : "GitHub Token 已保存（本机系统加密不可用，以明文+仅本人可读权限保存）",
    );
  };

  /** 选包：主进程只接受 .ccapp / .zip（community-github.cjs:844），这里按同一口径过滤。 */
  const pickPackage = async () => {
    const files = await window.chatcoderAPI?.selectFiles?.(
      [{ name: "微应用包", extensions: ["ccapp", "zip"] }],
    );
    const picked = files?.[0];
    if (picked) setPkgPath(picked);
  };

  const doPublish = async () => {
    const row = publishTarget;
    if (!row || !pkgPath) return;
    setPublishing(true);
    setProgress({ stage: "hashing", percent: 0 });
    setPublishResult(null);
    // 进度事件只带 stage/percent，不知道怎么对应到哪一行，所以展示在弹窗里跟着这次发布走
    unsubRef.current?.();
    unsubRef.current = communityBridge.onProgress((p) => setProgress(p));
    try {
      // manifest 交给主进程从库里读：它在打包/上传链路里本来就要用，前端复制一份只会带来不一致
      const detail = await api.getMicroApp(row.appId).catch(() => null);
      const repo = await resolveRepo();
      const res = await communityBridge.publish({
        appId: row.appId,
        version: row.version,
        filePath: pkgPath,
        manifest: detail?.manifest ?? undefined,
        desc: publishDesc.trim() || undefined,
        authorId: (await api.getCommunityIdentity().catch(() => null))?.userId,
        repo,
      });
      if (res.ok) {
        const timedOut = res.code === "TIMEOUT";
        setPublishResult({
          ok: true,
          text: timedOut
            ? communityErrorText("TIMEOUT", res.message)
            : `已发布到社区（issue #${res.issueNumber ?? "?"}）${res.comment ? `：${res.comment}` : ""}`,
          url: res.issueUrl,
        });
        // 索引有 CDN 缓存（D30）：刚发布最长 5 分钟不可见，刷新一次即可拿到 Actions 的写入结果
        await load({ refresh: true });
      } else {
        setPublishResult({ ok: false, text: communityErrorText(res.code, res.message) });
      }
    } catch (e) {
      setPublishResult({ ok: false, text: `发布失败：${String(e)}` });
    } finally {
      unsubRef.current?.();
      unsubRef.current = null;
      setPublishing(false);
    }
  };

  const doWithdraw = async () => {
    const row = withdrawTarget;
    setWithdrawTarget(null);
    if (!row) return;
    setWithdrawing(true);
    try {
      const repo = await resolveRepo();
      const res = await communityBridge.withdraw({
        appId: row.appId,
        version: row.version,
        authorId: (await api.getCommunityIdentity().catch(() => null))?.userId,
        repo,
      });
      if (res.ok) {
        notify.success(
          res.code === "TIMEOUT"
            ? communityErrorText("TIMEOUT", res.message)
            : `已撤回「${row.name}」${row.version}：只移除了社区索引条目，已发布的包仍然保留`,
        );
        await load({ refresh: true });
      } else {
        notify.error(communityErrorText(res.code, res.message));
      }
    } catch (e) {
      notify.error(`撤回失败：${String(e)}`);
    } finally {
      setWithdrawing(false);
    }
  };

  return (
    <div className="mkt-microapp">
      {/* 社区状态说明：网格上方一行提示。刷新不在这里——统一用拓展页顶栏那个按钮，
          它已通过 reloadToken 把信号递进来（MarketPanel 的 refresh，plan-693-2868），
          两处各放一个刷新按钮只会让位置与文案对不上。 */}
      <div className="mkt-microapp-hint">
        {community?.enabled === false
          ? "社区未配置：只显示本机库里的微应用（在「设置 → 拓展 → 微应用」里填社区仓库地址可打开社区）"
          : community?.stale
            ? `社区暂不可达，以下为上次缓存的内容（${community.staleSeconds ? `约 ${Math.round(community.staleSeconds / 60)} 分钟前` : "过期缓存"}）`
            : "本机已装的微应用可直接注入对话；社区条目点「安装」后进本机库"}
      </div>

      {communityError && (
        <div className="mkt-microapp-warn">无法读取社区：{communityError}（本机库不受影响）</div>
      )}

      {/* 外置清单读取失败：只提示这一次失败原因，不再解释「自带前后端与进程、目录在哪」——
          外置微应用在界面上就是一个普通微应用，用户不需要知道它的实现形态。 */}
      {extAvailable && extError && (
        <div className="mkt-microapp-warn">外置微应用清单读取失败：{describeExtError(extError)}</div>
      )}

      {/* 首屏骨架：形状与真实卡片一致（复用 market.css 的 .mkt-sk 系列），避免加载完成时整页跳动 */}
      {loading && visible.length === 0 && (
        <div className="mkt-grid" aria-busy="true" aria-label="正在加载">
          {Array.from({ length: 6 }, (_, i) => (
            <div className="mkt-card is-skeleton" key={i} style={{ ["--i" as string]: i }}>
              <div className="mkt-card-head">
                <span className="mkt-sk mkt-sk-ic" />
                <span className="mkt-card-title">
                  <span className="mkt-sk mkt-sk-line" style={{ width: "58%" }} />
                  <span className="mkt-sk mkt-sk-line is-sm" style={{ width: "32%" }} />
                </span>
              </div>
              <span className="mkt-sk mkt-sk-line" style={{ width: "96%" }} />
              <span className="mkt-sk mkt-sk-line" style={{ width: "72%" }} />
              <div className="mkt-card-foot">
                <span className="mkt-sk mkt-sk-pill" />
                <span className="mkt-sk mkt-sk-btn" />
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && visible.length === 0 && (
        <Empty
          title={query.trim() ? `没有匹配「${query.trim()}」的微应用` : "还没有微应用"}
          desc={query.trim()
            ? "换个关键词试试，或者在对话里让 AI 帮你创建一个"
            : "让 AI 帮你创建一个，或从设置 → 拓展里导入 .ccapp 包"}
        />
      )}

      {/* 卡片网格：与插件 / 技能 / 连接器同一套 .mkt-grid > .mkt-card（不另立卡片类）。
          rows 已把本机已装的排在前面、社区独有的排在后面。
          卡片本体的可点性按行区分：
           · 外置微应用 = 一个普通微应用，**整张卡片可点**即打开界面（卡片内没有任何按钮）；
           · 本机库 / 社区条目仍是「只让操作区响应」（它们没有详情弹窗，整卡可点会看起来像能点却没事发生）。 */}
      {visible.length > 0 && (
        <div className={"mkt-grid" + (loading ? " is-loading" : "")}>
          {visible.map((row, i) => {
            const openable = Boolean(row.external);
            return (
            <div
              key={row.key}
              className={"mkt-card" + (openable ? " is-openable" : "") + (extBusy === row.key ? " is-busy" : "")}
              style={{ ["--i" as string]: Math.min(i, 11) }}
              {...(openable ? {
                role: "button" as const,
                tabIndex: 0,
                "aria-label": `打开 ${row.name}`,
                onClick: () => void openExtInterface(row),
                onKeyDown: (e: ReactKeyboardEvent<HTMLDivElement>) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  void openExtInterface(row);
                },
              } : {})}
            >
              <div className="mkt-card-head">
                <span className="mkt-microapp-ic" aria-hidden>
                  <IconPuzzle size={22} />
                </span>
                <span className="mkt-card-title">
                  <Tooltip content={`${row.appId}@${row.version} · ${formatBytes(row.sizeBytes)}`}>
                    <span className="mkt-card-name">{row.name}</span>
                  </Tooltip>
                  {row.author && <span className="mkt-card-author">{row.author}</span>}
                </span>
              </div>
              <p className="mkt-card-desc">{row.description || "暂无说明"}</p>
              <div className="mkt-card-foot">
                <span className="mkt-card-tags">
                  <Badge size="xs" tone="neutral">v{row.version}</Badge>
                  <Badge size="xs" tone={SOURCE_BADGE[row.source].tone}>{SOURCE_BADGE[row.source].label}</Badge>
                  {row.published && <Badge size="xs" tone="success">已发布</Badge>}
                  {row.perms.length > 0 && (
                    <Tooltip content={`声明权限：${row.perms.join("、")}`}>
                      <Badge size="xs" tone="neutral">权限 {row.perms.length} 项</Badge>
                    </Tooltip>
                  )}
                </span>
                {/* 外置微应用不渲染操作区：它没有注入 / 发布 / 安装语义，点卡片就是唯一的操作 */}
                {!row.external && (
                <span className="mkt-card-ops">
                  {/* 底部只留**一个**主操作：已装 → 注入（最常用）；未装 → 安装。
                      其余动作（查看界面 / 发布 / 撤回）全部收进右侧「更多」菜单，
                      撤回是破坏性操作，放在菜单末尾并用分隔线隔开（AGENTS §2.1 第 5 条）。 */}
                  {row.installed ? (
                    <>
                      <Tooltip content="把微应用引用放进输入框（不打开右侧面板）">
                        <Button variant="secondary" size="xs" onClick={() => inject(row)}>
                          注入到输入框
                        </Button>
                      </Tooltip>
                      <Menu
                        align="end"
                        trigger={
                          <button type="button" className="ui-icon-btn size-xs" aria-label={`${row.name} 更多操作`}>
                            <IconMoreHorizontal size={15} />
                          </button>
                        }
                        entries={[
                          {
                            key: "open",
                            label: "查看界面",
                            icon: <IconBox size={12} />,
                            onSelect: () => openInterface(row),
                          },
                          ...(hasBridge ? [{
                            key: "publish",
                            label: "发布到社区",
                            icon: <IconArrowUpRight size={12} />,
                            disabled: community?.enabled === false,
                            divider: row.published,
                            onSelect: () => void openPublish(row),
                          }] : []),
                          ...(hasBridge && row.published ? [{
                            key: "withdraw",
                            label: "撤回（只移除索引，包保留）",
                            icon: <IconTrash size={12} />,
                            danger: true,
                            disabled: withdrawing,
                            onSelect: () => setWithdrawTarget(row),
                          }] : []),
                        ]}
                      />
                    </>
                  ) : (
                    <Tooltip content="下载并安装到本机微应用库（装完默认不打开界面）">
                      <Button
                        variant="primary"
                        size="xs"
                        icon={<IconDownload size={12} />}
                        loading={busy === row.key}
                        onClick={() => void installFromCommunity(row)}
                      >
                        安装
                      </Button>
                    </Tooltip>
                  )}
                </span>
                )}
              </div>
            </div>
            );
          })}
        </div>
      )}

      {/* ── 发布弹窗（含首次的 PAT 引导，D36）── */}
      <Modal
        open={publishTarget !== null}
        onClose={closePublish}
        title={`发布到社区${publishTarget ? `：${publishTarget.name} v${publishTarget.version}` : ""}`}
        subtitle="发布走 GitHub issue：软件把包传到你的中转 Release，再请社区仓库的 Actions 收录索引"
        width={620}
        footer={
          tokenSaved ? (
            <>
              <Button variant="ghost" size="sm" onClick={closePublish}>关闭</Button>
              <Button
                variant="primary"
                size="sm"
                loading={publishing}
                disabled={!pkgPath || publishing}
                onClick={() => void doPublish()}
              >
                发布
              </Button>
            </>
          ) : null
        }
      >
        {tokenSaved === null ? (
          // null = 还在查本机有没有存过 PAT：不先渲染发布表单，避免「刚打开就闪一下换成引导页」
          <div className="mkt-microapp-publish-note">正在检查本机的 GitHub Token…</div>
        ) : tokenSaved === false ? (
          // D36：首次发布才引导填 PAT，不放进设置页静默等待
          <div className="mkt-microapp-publish">
            <div className="mkt-microapp-publish-note">
              发布需要一个 <b>GitHub Personal Access Token</b>（勾选 <code>public_repo</code> 即可）：
              它只保存在本机（主进程加密存储），用来把你的包传到自己的仓库并提交一条发布 issue，
              不会上传到 ChatCoder 的服务器。
            </div>
            <div className="mkt-microapp-publish-row">
              <Input
                placeholder="ghp_… 或 github_pat_…"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                aria-label="GitHub Personal Access Token"
              />
              <Button variant="primary" size="sm" disabled={!tokenInput.trim()} onClick={() => void saveToken()}>
                保存
              </Button>
            </div>
            <Button
              variant="ghost"
              size="sm"
              icon={<IconExternalLink size={12} />}
              onClick={() => openExternal("https://github.com/settings/tokens/new?scopes=public_repo&description=ChatCoder")}
            >
              去 GitHub 创建 Token
            </Button>
          </div>
        ) : (
          <div className="mkt-microapp-publish">
            <div className="mkt-microapp-publish-row">
              <Input
                placeholder="选择要发布的 .ccapp / .zip 包"
                value={pkgPath}
                onChange={(e) => setPkgPath(e.target.value)}
                aria-label="微应用包路径"
              />
              <Button variant="secondary" size="sm" onClick={() => void pickPackage()}>选择文件</Button>
            </div>
            <div className="mkt-microapp-publish-row">
              <Input
                placeholder="一句话说明（可留空，默认用应用的说明）"
                value={publishDesc}
                onChange={(e) => setPublishDesc(e.target.value)}
                aria-label="发布说明"
              />
            </div>
            <div className="mkt-microapp-publish-note">
              包体积上限 100MB。发布后 Actions 需要几十秒到几分钟处理，索引有 CDN 缓存，最长 5 分钟内才能在列表看到。
            </div>
            {progress && (
              <div className="mkt-microapp-progress">
                <div className="mkt-microapp-progress-text">
                  {STAGE_TEXT[progress.stage] || progress.stage}
                  {typeof progress.percent === "number" && progress.stage === "uploading" ? ` ${progress.percent}%` : ""}
                </div>
                <div className="mkt-microapp-progress-track">
                  <span
                    className="mkt-microapp-progress-fill"
                    style={{ width: `${typeof progress.percent === "number" ? Math.max(2, Math.min(100, progress.percent)) : 12}%` }}
                  />
                </div>
                {progress.issueUrl && (
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={<IconExternalLink size={12} />}
                    onClick={() => openExternal(String(progress.issueUrl))}
                  >
                    查看发布 issue
                  </Button>
                )}
              </div>
            )}
            {publishResult && (
              <div className={"mkt-microapp-result" + (publishResult.ok ? " is-ok" : " is-err")}>
                {publishResult.text}
                {publishResult.url && (
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={<IconExternalLink size={12} />}
                    onClick={() => openExternal(String(publishResult.url))}
                  >
                    打开 issue
                  </Button>
                )}
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* ── 撤回确认（D35：只删索引条目，包保留）── */}
      <ConfirmDialog
        open={withdrawTarget !== null}
        title="从社区撤回"
        message={`将从社区索引里移除「${withdrawTarget?.name ?? ""}」${withdrawTarget?.version ? ` v${withdrawTarget.version}` : ""}。\n\n只移除索引条目，已经发布的包仍然保留（别人已有的下载链接不会失效）。此操作不可恢复（可重新发布）。`}
        confirmLabel="撤回"
        danger
        onCancel={() => setWithdrawTarget(null)}
        onConfirm={() => void doWithdraw()}
      />
    </div>
  );
}
