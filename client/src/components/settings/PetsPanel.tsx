/** 设置 · 宠物（plan-73-323 建立 / plan-73-326 重做排版与图库预览）。
 *
 * 排版对齐「设置 → 常规」：外层 settings-card-stack，每个分区一张 settings-card，
 * 设置项统一用共享 Row（标题 + 描述 + 右侧控件槽）——不再用 Card 组件自建卡片
 * （此前被外层 PANELS 的 card:true 再包一层，形成"卡片套卡片"）。
 *
 * 图库带缩略图：petdex 的 preview.webp 与精灵图都是「8 列帧图集」，
 * 展示时必须裁出**左上角第一帧**，否则卡片里会露出一整排 8 个帧、认不出是什么宠物。
 */
import { useEffect, useState } from "react";
import { Button, Input, List, ListRow, Slider, Switch } from "../ui";
import { Row } from "./shared";
import { usePetsStore } from "../../store/pets";
import type { GalleryPet } from "../../store/pets";
import type { InstalledPet } from "../../pet/petApi";

/** 图库每次展示条数（避免一次渲染上百项） */
const GALLERY_PAGE = 24;
/** 搜索防抖（AGENTS.md：设置项输入类 300ms 防抖） */
const SEARCH_DEBOUNCE_MS = 300;

function formatBytes(n: number): string {
  if (!n) return "";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 宠物缩略图：**取帧图集的左上角第一帧，等比缩放居中显示**。
 *
 * 实测取数（plan-73-344，30 只抽样）：preview.webp 全部是 **8 列 × 1 行**（1152×208，
 * 单帧 144×208）；精灵图是 8 列 × 9 行（1536×1872，单帧 192×208）。两者都只是帧图集。
 *
 * 实现方式（为什么不是"放大 8 倍贴左上角"）：
 *   旧写法用 `background-size: calc(100% * 8)` 把背景放大后只露左上角，**前提是容器宽高比
 *   恰好等于单帧比例**（144:208）。一旦容器因布局被压扁，就会切掉宠物上下部分
 *   （用户反馈"展示不全被遮挡"）。那条路径把"完整显示"寄托在容器尺寸上，本身就脆弱。
 *   现改为：
 *     ① `object-view-box: inset(0 87.5% …)` —— 由浏览器按**图像自身坐标**精确裁出第 1 帧
 *        （右侧裁掉 87.5% = 只留 1/8 列），与容器尺寸完全解耦；
 *     ② `object-fit: contain` —— 由浏览器保证**等比缩放、完整显示、居中**，永不裁剪变形。
 *   两者叠加即"等比不变形 + 居中 + 不裁剪"，不再依赖任何尺寸匹配假设。
 */
function PetThumb({ pet, width }: { pet: Pick<GalleryPet, "previewUrl" | "spritesheetUrl">; width?: number }) {
  const preview = pet.previewUrl || "";
  const sheet = pet.spritesheetUrl || "";
  /** 当前实际使用的图源（官方预览图不可用时回退精灵图） */
  const [src, setSrc] = useState(preview || sheet);
  /** 当前用的是精灵图吗（精灵图 9 行，需额外裁掉下方） */
  const [isSheet, setIsSheet] = useState(!preview && !!sheet);
  const [state, setState] = useState<"loading" | "ok" | "failed">(preview || sheet ? "loading" : "failed");

  useEffect(() => {
    const first = preview || sheet;
    if (!first) {
      setSrc("");
      setState("failed");
      return;
    }
    setSrc(first);
    setIsSheet(!preview && !!sheet);
    setState("loading");
  }, [preview, sheet]);

  /** 首图失败 → 回退精灵图；都失败则显示占位。
   *  （用 <img> 的 onError 天然拿到失败信号，无需再预载探测。） */
  const handleError = () => {
    if (!isSheet && sheet && sheet !== src) {
      setSrc(sheet);
      setIsSheet(true);
      setState("loading");
      return;
    }
    setState("failed");
  };

  // 传 width 时用固定正方形（已安装列表小图标）：需同时清掉 padding-bottom 撑高，
  // 否则 height 与 padding 叠加会变成两倍高
  const style = width
    ? ({ width: `${width}px`, height: `${width}px`, paddingBottom: 0 } as React.CSSProperties)
    : undefined;
  return (
    <span className="pets-thumb" style={style} data-state={state}>
      {state !== "failed" && (
        <img
          src={src}
          alt=""
          loading="lazy"
          draggable={false}
          className={isSheet ? "is-sheet" : undefined}
          onLoad={() => setState("ok")}
          onError={handleError}
        />
      )}
      {state === "failed" && <span className="pets-thumb-empty" aria-hidden="true" />}
    </span>
  );
}

export function PetsPanel() {
  const pref = usePetsStore((s) => s.pref);
  const installed = usePetsStore((s) => s.installed);
  const installedLoading = usePetsStore((s) => s.installedLoading);
  const gallery = usePetsStore((s) => s.gallery);
  const galleryTotal = usePetsStore((s) => s.galleryTotal);
  const galleryLoading = usePetsStore((s) => s.galleryLoading);
  const galleryError = usePetsStore((s) => s.galleryError);
  const galleryFromCache = usePetsStore((s) => s.galleryFromCache);
  const busySlug = usePetsStore((s) => s.busySlug);
  const actionError = usePetsStore((s) => s.actionError);
  const init = usePetsStore((s) => s.init);
  const setPref = usePetsStore((s) => s.setPref);
  const loadGallery = usePetsStore((s) => s.loadGallery);
  const install = usePetsStore((s) => s.install);
  const remove = usePetsStore((s) => s.remove);
  const importLocal = usePetsStore((s) => s.importLocal);
  const showPet = usePetsStore((s) => s.showPet);

  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(GALLERY_PAGE);

  useEffect(() => {
    void init();
  }, [init]);

  // 偏好广播：宠物窗口侧改动（如手动隐藏）时保持同步
  useEffect(() => {
    const off = window.chatcoderAPI?.onPetPrefChanged?.((p) => usePetsStore.getState().syncPref(p));
    return () => {
      off?.();
    };
  }, []);

  // 搜索防抖（空串也执行一次，作为首屏图库加载）
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setLimit(GALLERY_PAGE);
      void loadGallery({ query });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, loadGallery]);

  const shown = gallery.slice(0, limit);
  const current: InstalledPet | null = installed.find((p) => p.slug === pref?.slug) || null;
  const hidden = pref?.visible === false;

  return (
    <div className="settings-card-stack">
      {actionError && <div className="navpage-empty">操作失败：{actionError}</div>}

      <div className="settings-card">
        <Row title="在桌面显示宠物" desc="宠物常驻桌面（置顶、不占任务栏），实时反映任务执行状况">
          <Switch checked={!!pref?.enabled} onChange={(v) => void setPref({ enabled: v })} />
        </Row>
        {hidden && (
          <Row title="宠物已隐藏" desc="通过宠物下方角标隐藏后，可从这里重新显示">
            <Button size="sm" variant="primary" onClick={() => void showPet()}>
              重新显示
            </Button>
          </Row>
        )}
        <Row
          title="当前宠物"
          desc={
            current
              ? `${current.displayName} · ${current.slug}`
              : installed.length > 0
                ? "尚未选择，请在下方「已安装」中设为当前"
                : "尚未安装宠物，请在下方图库中安装或从本地导入"
          }
        >
          {current ? (
            <>
              <Button size="sm" onClick={() => void window.chatcoderAPI?.petOpenPetPage?.(current.slug)}>
                来源
              </Button>
              <Button size="sm" onClick={() => void window.chatcoderAPI?.petRevealPet?.(current.slug)}>
                文件夹
              </Button>
              <Button size="sm" variant="danger-ghost" loading={busySlug === current.slug} onClick={() => void remove(current.slug)}>
                移除
              </Button>
            </>
          ) : null}
        </Row>
      </div>

      <div className="settings-card">
        <Row title="宠物大小" desc="也可直接在桌面上拖拽宠物右下角角标缩放">
          <Slider
            min={0.6}
            max={1.6}
            step={0.1}
            value={pref?.scale ?? 1}
            onChange={(v) => void setPref({ scale: v })}
            format={(v) => `${Math.round(Number(v) * 100)}%`}
          />
        </Row>
        <Row title="悬停显示任务浮窗" desc="鼠标移到宠物上时，浮窗展开显示运行中的任务与实时进度">
          <Switch checked={pref?.showCapsule !== false} onChange={(v) => void setPref({ showCapsule: v })} />
        </Row>
        <Row title="浮窗最多显示任务数" desc="未悬停时只显示最新一条，悬停后按此数量展开，其余折叠为「+N」">
          <Slider
            min={1}
            max={3}
            step={1}
            value={pref?.maxCapsuleRows ?? 3}
            onChange={(v) => void setPref({ maxCapsuleRows: v })}
            format={(v) => `${v} 个`}
          />
        </Row>
        <Row
          title="任务顺序"
          desc={
            (pref?.blockOrder?.length ?? 0) > 0
              ? "已按你在浮窗里拖拽的顺序显示，新任务会追加在后面"
              : "默认按运行状态与开始时间排序；在浮窗里按住任务块上下拖动即可自定义"
          }
        >
          {(pref?.blockOrder?.length ?? 0) > 0 ? (
            <Button size="sm" onClick={() => void setPref({ blockOrder: [] })}>
              恢复自动排序
            </Button>
          ) : (
            <span />
          )}
        </Row>
        <Row title="显示任务数量徽标" desc="同时运行 2 个以上任务时，在宠物旁显示数量">
          <Switch checked={pref?.showBadge !== false} onChange={(v) => void setPref({ showBadge: v })} />
        </Row>
        <Row title="点击穿透" desc="宠物周围的透明区域不挡住桌面点击（推荐开启）">
          <Switch checked={pref?.clickThrough !== false} onChange={(v) => void setPref({ clickThrough: v })} />
        </Row>
        <Row title="主窗最小化时隐藏宠物" desc="避免宠物留在桌面上影响其他工作">
          <Switch checked={pref?.hideOnMinimize !== false} onChange={(v) => void setPref({ hideOnMinimize: v })} />
        </Row>
      </div>

      <div className="settings-card">
        <Row title={`已安装（${installed.length}）`} desc="点击宠物可设为当前；损坏的资源建议移除后重新安装">
          <Button size="sm" variant="primary" onClick={() => void importLocal()}>
            从本地导入
          </Button>
        </Row>
        <List>
          {installed.map((p) => (
            <ListRow
              key={p.slug}
              name={
                <span className="pets-installed-name">
                  <PetThumb
                  pet={{
                    // 本地导入的宠物（slug 带 local- 前缀）没有 petdex 预览图，显示占位即可
                    previewUrl: p.slug.startsWith("local-")
                      ? ""
                      : `https://assets.petdex.dev/pets/${encodeURIComponent(p.slug)}/preview.webp`,
                    spritesheetUrl: "",
                  }}
                  width={34}
                />
                  <span>{p.displayName}</span>
                </span>
              }
              desc={[p.slug, p.rows === 11 ? "v2 图集" : "标准图集", p.bytes ? formatBytes(p.bytes) : "", p.broken ? "资源损坏，建议移除" : ""]
                .filter(Boolean)
                .join(" · ")}
              actions={
                <>
                  <Button
                    size="xs"
                    variant={pref?.slug === p.slug ? "subtle" : "outline"}
                    disabled={pref?.slug === p.slug || !!p.broken}
                    onClick={() => void setPref({ slug: p.slug, enabled: true, visible: true })}
                  >
                    {pref?.slug === p.slug ? "使用中" : "设为当前"}
                  </Button>
                  <Button size="xs" variant="danger-ghost" loading={busySlug === p.slug} onClick={() => void remove(p.slug)}>
                    移除
                  </Button>
                </>
              }
            />
          ))}
          {installed.length === 0 && !installedLoading ? <div className="navpage-empty">尚未安装宠物</div> : null}
        </List>
      </div>

      <div className="settings-card">
        <Row
          title="宠物图库"
          desc={
            galleryError
              ? `获取失败（${galleryError}）`
              : galleryFromCache
                ? `当前为缓存列表，共 ${galleryTotal} 只`
                : `来自 petdex 公共图库，共 ${galleryTotal} 只`
          }
        >
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索名称或标识"
            aria-label="搜索宠物"
          />
          <Button loading={galleryLoading} onClick={() => void loadGallery({ force: true, query })}>
            刷新
          </Button>
        </Row>

        {shown.length > 0 ? (
          <div className="pets-gallery">
            {shown.map((p) => (
              <div className={`pets-card${p.installed ? " is-installed" : ""}`} key={p.slug}>
                <PetThumb pet={p} />
                <div className="pets-card-name" title={p.displayName}>{p.displayName}</div>
                <div className="pets-card-meta" title={[p.author, p.kind].filter(Boolean).join(" · ")}>
                  {p.author || p.kind || p.slug}
                </div>
                {p.installed ? (
                  <Button size="xs" variant="subtle" disabled>已安装</Button>
                ) : (
                  <Button size="xs" variant="primary" loading={busySlug === p.slug} onClick={() => void install(p.slug)}>
                    安装
                  </Button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="navpage-empty">{galleryLoading ? "正在加载图库…" : query ? "没有匹配的宠物" : "图库为空，可点击刷新重试"}</div>
        )}

        {gallery.length > limit && (
          <div className="pets-gallery-more">
            <Button block onClick={() => setLimit((n) => n + GALLERY_PAGE)}>
              加载更多（还有 {gallery.length - limit} 只）
            </Button>
          </div>
        )}
      </div>

      <div className="settings-card">
        <Row
          title="素材来源与版权"
          desc="宠物素材来自 petdex 社区投稿，版权归各作者所有。本应用仅在本地缓存所选宠物，不做二次分发；如权利人提出异议，可直接移除本地文件。"
        >
          <span />
        </Row>
      </div>
    </div>
  );
}
