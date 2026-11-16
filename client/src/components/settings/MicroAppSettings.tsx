/** 设置 → 拓展 → 微应用：库位置 / 社区仓库 / 拉黑管理（plan-693-2868 §7 P3-3、P5-6、D16、D8）。
 *
 *  为什么放在「设置 → 拓展」而不单开一页：D1 定的就是「拓展第四类」——微应用的库、社区、
 *  拉黑都是**拓展体系的配置**，与宠物/电脑操控那些独立能力页不是一类东西；拆成新页反而让
 *  用户找不到「装了微应用之后去哪儿配」。
 *
 *  三项都遵循项目约定「修改即保存」：输入类 300ms 防抖，开关/按钮即时生效。
 *  库位置只读展示（后端 `microapp_root()` 是权威解析链：settings → 环境变量 → 默认），
 *  前端不拼路径——拼一份必然与工具面漂移（§6.1 双根统一的教训）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type CommunityBlocklistOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { notify } from "../../store/toast";
import { Button, Collapse, Empty, Input, Tooltip } from "../ui";
import { IconCopy, IconExternalLink, IconFolderOpen, IconRefresh, IconTrash } from "../icons";

function fail(msg: string) { useChatStore.setState({ error: msg }); }

export function MicroAppSettings() {
  /** 微应用库根（后端 `GET /api/microapps` 的 root，与工具面同一份解析链） */
  const [root, setRoot] = useState("");
  const [rootError, setRootError] = useState<string | null>(null);
  /** 社区仓库地址（`community_repo`，空 = 关闭社区、隐藏所有社区入口，§7 P5-8） */
  const [repo, setRepo] = useState("");
  const [repoLoaded, setRepoLoaded] = useState(false);
  const saveTimer = useRef<number | null>(null);
  const pendingRepo = useRef<string | null>(null);
  /** 拉黑表（D16 两档）：users 按作者、apps 按条目 */
  const [blocklist, setBlocklist] = useState<CommunityBlocklistOut>({ users: [], apps: [] });
  const [blocklistOpen, setBlocklistOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  // ── 库位置 ──
  const loadRoot = useCallback(async () => {
    try {
      const res = await api.listMicroApps();
      setRoot(res.root || "");
      setRootError(null);
    } catch (e) {
      setRoot("");
      setRootError(String(e));
    }
  }, []);
  useEffect(() => { void loadRoot(); }, [loadRoot]);

  // ── 社区仓库地址 ──
  useEffect(() => {
    api.getGlobalSettings()
      .then((s) => { setRepo(s.community_repo ?? ""); setRepoLoaded(true); })
      .catch((e) => { setRepoLoaded(true); fail("读取社区仓库设置失败：" + String(e)); });
  }, []);

  /** 「修改即保存」：300ms 防抖写 settings；卸载时补发（与 GeneralPanel 同一套做法） */
  const flushRepo = useCallback(async (value: string) => {
    try {
      await api.setGlobalSettings({ community_repo: value.trim() });
      // 社区开关同时影响市场页的社区区块：清单里 enabled=false 时入口自动隐藏
      notify.success(value.trim() ? `社区仓库已设为 ${value.trim()}` : "社区已关闭（仓库地址已清空）");
    } catch (e) {
      fail("保存社区仓库失败：" + String(e));
    }
  }, []);
  const scheduleRepo = useCallback((value: string) => {
    pendingRepo.current = value;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null;
      const v = pendingRepo.current;
      pendingRepo.current = null;
      if (v !== null) void flushRepo(v);
    }, 300);
  }, [flushRepo]);
  useEffect(() => () => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    const v = pendingRepo.current;
    pendingRepo.current = null;
    if (v !== null) void flushRepo(v);
  }, [flushRepo]);

  // ── 拉黑管理 ──
  const loadBlocklist = useCallback(async () => {
    try {
      setBlocklist(await api.getCommunityBlocklist());
    } catch (e) {
      // 社区未配置 / 不可达时静默：拉黑表读不到不该在设置页弹错（它只影响社区列表）
      notify.info("暂未读到社区拉黑表：" + String(e));
    }
  }, []);
  useEffect(() => { if (blocklistOpen) void loadBlocklist(); }, [blocklistOpen, loadBlocklist]);

  /** 解除一条拉黑：PUT 全量提交时**只改这一档**，另一档传 undefined 表示不动。 */
  const unblock = async (dim: "users" | "apps", value: string) => {
    setBusy(`${dim}:${value}`);
    try {
      const next = {
        users: dim === "users" ? blocklist.users.filter((x) => x !== value) : undefined,
        apps: dim === "apps" ? blocklist.apps.filter((x) => x !== value) : undefined,
      };
      setBlocklist(await api.updateCommunityBlocklist(next));
      notify.success(`已解除屏蔽：${value}`);
    } catch (e) {
      fail("解除屏蔽失败：" + String(e));
    } finally {
      setBusy(null);
    }
  };

  const openDir = async () => {
    if (!root) return;
    const res = await window.chatcoderAPI?.openPath?.(root);
    // openPath 返回空串表示成功（主进程约定）；非空是失败原因
    if (typeof res === "string" && res) fail(`打开目录失败：${res}`);
  };

  const copyRoot = () => {
    void navigator.clipboard.writeText(root).then(
      () => notify.success("库路径已复制"),
      () => fail("复制失败：浏览器未授权剪贴板"),
    );
  };

  const hasBlocked = blocklist.users.length + blocklist.apps.length > 0;

  return (
    <div className="ui-card mas">
      {/* ① 微应用库位置（P3-3）：显示 + 打开 + 复制；根由后端解析，前端不拼 */}
      <div className="ui-card-row">
        <div className="ui-card-row-info">
          <div className="ui-card-row-title">微应用库位置</div>
          <div className="ui-card-row-desc">
            默认 <code>~/.chatcoder/microapps</code>，可用设置项或环境变量改；
            升版本不会动这里已有的文件。
          </div>
          <div className="mas-path mono">
            {root || (rootError ? `读取失败：${rootError}` : "读取中…")}
          </div>
        </div>
        <div className="ui-card-row-control">
          <Tooltip content="在本机文件管理器里打开库目录">
            <Button variant="secondary" size="sm" icon={<IconFolderOpen size={13} />} disabled={!root} onClick={() => void openDir()}>
              打开目录
            </Button>
          </Tooltip>
          <Tooltip content="复制库路径">
            <Button variant="ghost" size="sm" icon={<IconCopy size={13} />} disabled={!root} onClick={copyRoot}>
              复制
            </Button>
          </Tooltip>
          <Tooltip content="重新读取库位置">
            <Button variant="ghost" size="sm" icon={<IconRefresh size={13} />} onClick={() => void loadRoot()}>
              刷新
            </Button>
          </Tooltip>
        </div>
      </div>

      {/* ② 社区仓库地址（P5-8）：空 = 关闭社区 */}
      <div className="ui-card-row">
        <div className="ui-card-row-info">
          <div className="ui-card-row-title">社区仓库地址</div>
          <div className="ui-card-row-desc">
            形如 <code>owner/chatcoder-microapps</code>。留空 = 关闭社区（拓展页不再出现任何社区内容）。
            所有人读同一个仓库，发布走 issue，不需要给谁加写权限。
          </div>
        </div>
        <div className="ui-card-row-control">
          <Input
            className="mas-input"
            placeholder="owner/chatcoder-microapps"
            value={repo}
            disabled={!repoLoaded}
            onChange={(e) => { setRepo(e.target.value); scheduleRepo(e.target.value); }}
            aria-label="社区仓库地址"
          />
          <Tooltip content="在浏览器打开这个社区仓库">
            <Button
              variant="ghost"
              size="sm"
              icon={<IconExternalLink size={13} />}
              disabled={!repo.trim()}
              onClick={() => {
                const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
                void w.chatcoderAPI?.openExternal?.(`https://github.com/${repo.trim()}`);
              }}
            >
              打开
            </Button>
          </Tooltip>
        </div>
      </div>

      {/* ③ 拉黑管理（D16）：两档都要能解除 */}
      <div className="ui-card-row">
        <div className="ui-card-row-info">
          <div className="ui-card-row-title">拉黑管理</div>
          <div className="ui-card-row-desc">
            按作者或按条目屏蔽社区内容：按作者会过滤该作者的**全部**条目，按条目只过滤单条。
            只影响社区列表与安装，不影响已经装到本机的副本。
          </div>
        </div>
        <div className="ui-card-row-control">
          {/* 展开/收起用按钮而不是开关：开关表达的是「一个持久化的开/关设置」，
              而这里只是把已有的拉黑表展开看看，拨回去并不代表「不拉黑」——用 Switch 会误导。 */}
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setBlocklistOpen((v) => !v)}
          >
            {blocklistOpen ? "收起" : "查看与解除"}
          </Button>
        </div>
      </div>
      <Collapse open={blocklistOpen} className="in-gap">
        <div className="mas-blocklist">
          {!hasBlocked && (
            // 空态用统一组件（自带嗒嗒与标题/说明两行的节奏），不在这里另写一段说明文案
            <Empty
              size="sm"
              title="还没有屏蔽任何作者或条目"
              desc="解除后立即重新出现在社区列表里；只影响社区列表与安装，不影响已装到本机的副本。"
            />
          )}
          {blocklist.users.length > 0 && (
            <div className="mas-blocklist-group">
              <div className="mas-blocklist-title">按作者（{blocklist.users.length}）</div>
              {blocklist.users.map((u) => (
                <div key={`u:${u}`} className="mas-blocklist-row">
                  <span className="mono">{u}</span>
                  <Button
                    variant="ghost"
                    size="xs"
                    icon={<IconTrash size={12} />}
                    disabled={busy === `users:${u}`}
                    onClick={() => void unblock("users", u)}
                  >
                    解除
                  </Button>
                </div>
              ))}
            </div>
          )}
          {blocklist.apps.length > 0 && (
            <div className="mas-blocklist-group">
              <div className="mas-blocklist-title">按条目（{blocklist.apps.length}）</div>
              {blocklist.apps.map((a) => (
                <div key={`a:${a}`} className="mas-blocklist-row">
                  <span className="mono">{a}</span>
                  <Button
                    variant="ghost"
                    size="xs"
                    icon={<IconTrash size={12} />}
                    disabled={busy === `apps:${a}`}
                    onClick={() => void unblock("apps", a)}
                  >
                    解除
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </Collapse>
    </div>
  );
}
