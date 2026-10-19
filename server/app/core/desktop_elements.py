"""电脑操控的元素清单（plan-20261019-desktop-targeting）。

**为什么需要它**：模型给的「坐标」是整条链路里最不可靠的一环——实录里同一个模型（deepseek-flash）在
「图上像素」口径下点得很准，换成 0–1000 比例后因为要心算换算，x 轴换了、y 轴没换，点到标题文字上还以为
点到了输入框。**能不给坐标就不给坐标**：把窗口里的可交互元素编号成 e1…eN 列给模型，模型只需要说「点 e3」
或「点『登录』」，位置由内核从无障碍树（UI Automation）取到的物理像素矩形给出——与截图、点击同一坐标系，
和屏幕缩放比例、分辨率完全无关。

元素来源：原生应用走 UIA；Chrome / Edge / Electron 等 Chromium 系窗口同样走 UIA
（内核会先触发网页无障碍树的构建再取节点，不需要调试端口）；有调试端口时 CDP 作为补充。

本模块只做纯计算：筛选 / 排序 / 编号清单文本 / 按文字或编号定位 / 点击点附近的候选 / 命中元素描述，
不接触内核与图像，可完整单测。
"""
from __future__ import annotations

from typing import Sequence

# 清单上限：可交互元素全部优先，文字补满。再多只会撑大上下文、让模型在清单里迷路
LIST_LIMIT = 90
# 动作后自动附带的清单更短（每步都会附带，累积上下文）
AFTER_ACTION_LIMIT = 60
# 文字（非可交互）元素只收短标签：长段落没有「点」的意义
TEXT_MAX_LEN = 30
TEXT_LIMIT = 40
# 太小的矩形多是布局碎片或装饰
MIN_SIZE = 4
# 点击点「附近」的搜索半径（屏幕物理像素）
NEAR_MAX_PX = 360

# 控件类型的中文简称：比英文 ControlType 更直观，也更省 token
CT_LABEL = {
    "Button": "按钮", "Edit": "输入框", "Hyperlink": "链接", "CheckBox": "复选框",
    "RadioButton": "单选框", "ComboBox": "下拉框", "ListItem": "列表项", "MenuItem": "菜单项",
    "TabItem": "标签页", "TreeItem": "树节点", "SplitButton": "按钮", "Slider": "滑块",
    "Spinner": "数值框", "DataItem": "数据项", "HeaderItem": "表头", "Text": "文字",
    "Image": "图片", "Custom": "自定义控件", "Group": "分组", "List": "列表", "Tab": "标签栏",
    "Menu": "菜单", "ProgressBar": "进度条", "Tree": "树", "Table": "表格", "Document": "文档",
    "Pane": "面板", "Window": "窗口", "ToolBar": "工具栏", "StatusBar": "状态栏",
}

# 命中元素是这些类型且没有名称时，视为「没有可识别的控件」（点到了空白 / 容器 / 自绘区域）
_BLANK_CT = {"Pane", "Window", "Document", "Group", "Custom", "TitleBar", ""}


# ── 基本读取 ─────────────────────────────────────────────────────


def element_name(item: dict) -> str:
    return str(item.get("n") or item.get("name") or "").strip()


def element_rect(item: dict) -> tuple[int, int, int, int] | None:
    r = item.get("r")
    if isinstance(r, (list, tuple)) and len(r) >= 4:
        try:
            x, y, w, h = int(r[0]), int(r[1]), int(r[2]), int(r[3])
        except (TypeError, ValueError):
            return None
        if w > 0 and h > 0:
            return (x, y, w, h)
    return None


def element_center(item: dict) -> tuple[int, int] | None:
    rect = element_rect(item)
    if rect is not None:
        x, y, w, h = rect
        return (x + w // 2, y + h // 2)
    try:
        if item.get("x") is not None and item.get("y") is not None:
            return (int(item["x"]), int(item["y"]))
    except (TypeError, ValueError):
        pass
    return None


def is_interactive(item: dict) -> bool:
    """可交互：内核 / CDP 标了 ix，或控件类型天然可点。缺字段（老内核）按可交互处理。"""
    if "ix" in item:
        return bool(item.get("ix"))
    return str(item.get("ct") or "") not in ("Text", "Image", "Document", "Pane", "Window")


def label_of(item: dict) -> str:
    """清单里显示的名称：优先元素自身名称，其次内部文字（_label，见 prepare），最后占位。"""
    return element_name(item) or str(item.get("_label") or "").strip() or "(无名称)"


def type_label(item: dict) -> str:
    ct = str(item.get("ct") or "")
    return CT_LABEL.get(ct) or ct or "元素"


def norm_text(text: str) -> str:
    """文字比较口径：去掉全部空白与常见标点、转小写——「登 录」「登录」「登录：」视为同一个。"""
    s = "".join(str(text or "").split()).lower()
    for ch in ("：", ":", "（", "）", "(", ")", "*", "＊", "·", "…", "."):
        s = s.replace(ch, "")
    return s


# ── 几何 ─────────────────────────────────────────────────────────


def _intersects(rect: tuple[int, int, int, int], region: Sequence[int]) -> bool:
    x, y, w, h = rect
    rx, ry, rw, rh = int(region[0]), int(region[1]), int(region[2]), int(region[3])
    return not (x + w <= rx or x >= rx + rw or y + h <= ry or y >= ry + rh)


def _center_inside(rect: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    return outer[0] <= cx <= outer[0] + outer[2] and outer[1] <= cy <= outer[1] + outer[3]


def dist_to_rect(rect: tuple[int, int, int, int], x: int, y: int) -> float:
    rx, ry, rw, rh = rect
    dx = max(rx - x, 0, x - (rx + rw))
    dy = max(ry - y, 0, y - (ry + rh))
    return (dx * dx + dy * dy) ** 0.5


# ── 筛选 / 排序 ──────────────────────────────────────────────────


def prepare(
    items: Sequence[dict], region: Sequence[int] | None = None, *, limit: int = LIST_LIMIT,
) -> list[dict]:
    """把原始元素整理成清单：落在区域内 → 去重 → 去掉被可交互元素覆盖的文字 → 封顶 → 按阅读顺序输出。

    - 可交互元素优先保留，其余用「短文字」补满（长段落不收）；
    - 链接 / 按钮里面的文字子节点与父元素重复，丢掉；父元素没有名称时把内部文字借给它当名称；
    - 有网页内容（d=1）的窗口，网页内容排在前面，浏览器 / 窗口自身的界面排在后面，编号随清单顺序。
    """
    seen: set[tuple[str, int, int, int, int]] = set()
    inter: list[tuple[int, dict, tuple[int, int, int, int], str]] = []
    texts: list[tuple[int, dict, tuple[int, int, int, int], str]] = []
    for idx, it in enumerate(items):
        rect = element_rect(it)
        if rect is None or rect[2] < MIN_SIZE or rect[3] < MIN_SIZE:
            continue
        if region is not None and not _intersects(rect, region):
            continue
        name = element_name(it)
        ix = is_interactive(it)
        if not ix and not name:
            continue
        key = (name, rect[0], rect[1], rect[2], rect[3])
        if key in seen:
            continue
        seen.add(key)
        (inter if ix else texts).append((idx, it, rect, name))

    # 文字去重 / 借名
    kept_texts: list[tuple[int, dict, tuple[int, int, int, int], str]] = []
    borrowed: set[int] = set()
    for tidx, tit, trect, tname in texts:
        if len(tname) > TEXT_MAX_LEN:
            continue
        covered = False
        for _i, iit, irect, iname in inter:
            if not _center_inside(trect, irect):
                continue
            if not iname:
                # 父元素没有名称：这段文字就是它的标签，借给它（每个元素最多借一次）
                if id(iit) not in borrowed:
                    iit["_label"] = tname
                    borrowed.add(id(iit))
                covered = True
                break
            a, b = norm_text(tname), norm_text(iname)
            if a and b and (a in b or b in a):
                covered = True
                break
        if not covered:
            kept_texts.append((tidx, tit, trect, tname))

    chosen = list(inter[:limit])
    room = limit - len(chosen)
    if room > 0:
        chosen += kept_texts[: min(room, TEXT_LIMIT)]
    chosen.sort(key=lambda t: t[0])

    out: list[dict] = []
    for _idx, it, _rect, _name in chosen:
        copy = dict(it)
        copy["ix"] = is_interactive(it)
        out.append(copy)
    if any(it.get("d") for it in out):
        out = [it for it in out if it.get("d")] + [it for it in out if not it.get("d")]
    return out


# ── 清单文本 ─────────────────────────────────────────────────────


def format_line(ref: str, item: dict) -> str:
    """一行：编号 + 类型 +「名称」+ 状态。不列坐标——点击直接用编号或文字。"""
    flags: list[str] = []
    if not item.get("en", True):
        flags.append("已禁用")
    elif item.get("editable") or str(item.get("ct") or "") == "Edit":
        flags.append("可输入")
    val = str(item.get("val") or "").strip()
    if val and (item.get("editable") or str(item.get("ct") or "") == "Edit"):
        flags.append(f"当前值「{val[:20]}」")
    suffix = f"  （{'，'.join(flags)}）" if flags else ""
    return f"{ref:<4}{type_label(item)}「{label_of(item)}」{suffix}"


def duplicate_context(mapping: dict[str, dict]) -> dict[str, str]:
    """同名元素的区分线索：表格里每行都有「编辑」「收费」，只看名称分不出是哪一行。

    给名称重复的元素补一句「紧跟在「X」之后」——X 是清单顺序里它前面最近的、名称不同的元素
    （表格行里通常是该行的患者名 / 单号），模型据此挑编号。名称唯一的元素不加，省 token。
    """
    refs = list(mapping)
    counts: dict[tuple[str, str], int] = {}
    for it in mapping.values():
        key = (type_label(it), norm_text(label_of(it)))
        counts[key] = counts.get(key, 0) + 1
    out: dict[str, str] = {}
    for i, ref in enumerate(refs):
        it = mapping[ref]
        if counts.get((type_label(it), norm_text(label_of(it))), 0) < 2:
            continue
        me = norm_text(label_of(it))
        for j in range(i - 1, -1, -1):
            prev = label_of(mapping[refs[j]])
            if prev != "(无名称)" and norm_text(prev) != me:
                out[ref] = f"紧跟在「{prev[:14]}」之后"
                break
    return out


def format_list(mapping: dict[str, dict]) -> list[str]:
    """编号清单文本。有网页内容时分「网页内容 / 窗口界面」两组，便于模型一眼找到页面里的东西。"""
    ctx = duplicate_context(mapping)

    def line(ref: str, it: dict) -> str:
        base = format_line(ref, it)
        return f"{base}  〔{ctx[ref]}〕" if ref in ctx else base

    has_doc = any(it.get("d") for it in mapping.values())
    if not has_doc:
        return [line(ref, it) for ref, it in mapping.items()]
    lines: list[str] = []
    page = [(r, it) for r, it in mapping.items() if it.get("d")]
    frame = [(r, it) for r, it in mapping.items() if not it.get("d")]
    if page:
        lines.append("【网页内容】")
        lines.extend(line(r, it) for r, it in page)
    if frame:
        lines.append("【浏览器 / 窗口界面】")
        lines.extend(line(r, it) for r, it in frame)
    return lines


# ── 按文字定位 ───────────────────────────────────────────────────


def find_by_text(mapping: dict[str, dict], text: str) -> list[tuple[str, dict]]:
    """按元素上的文字找：完全一致优先，其次互相包含（至少 2 个字符）。同级里可交互元素排前面。"""
    want = norm_text(text)
    if not want:
        return []
    exact: list[tuple[str, dict]] = []
    partial: list[tuple[str, dict]] = []
    for ref, it in mapping.items():
        name = norm_text(label_of(it) if label_of(it) != "(无名称)" else "")
        val = norm_text(str(it.get("val") or ""))
        if not name and not val:
            continue
        if want == name or (val and want == val):
            exact.append((ref, it))
        elif len(want) >= 2 and ((name and (want in name or (len(name) >= 2 and name in want)))
                                 or (val and want in val)):
            partial.append((ref, it))

    def rank(pair: tuple[str, dict]) -> int:
        return 0 if is_interactive(pair[1]) else 1

    exact.sort(key=rank)
    partial.sort(key=rank)
    return exact or partial


def looks_like_ref(text: str) -> bool:
    """e5 / E5 / 5 这类写法按编号处理，其它按文字处理。"""
    s = str(text or "").strip().lower()
    if s.startswith("e") and s[1:].isdigit():
        return True
    return s.isdigit()


# ── 点击点附近的候选 / 命中元素描述 ───────────────────────────────


def _direction(dx: float, dy: float) -> str:
    if abs(dy) >= abs(dx):
        return "下方" if dy > 0 else "上方"
    return "右侧" if dx > 0 else "左侧"


def nearest(
    mapping: dict[str, dict], x: int, y: int, *, k: int = 3, max_dist: int = NEAR_MAX_PX,
) -> list[tuple[str, dict, str]]:
    """离 (x, y) 最近的可交互元素（含 方向词），按距离升序，最多 k 个。"""
    cands: list[tuple[float, str, dict, str]] = []
    for ref, it in mapping.items():
        if not is_interactive(it):
            continue
        rect = element_rect(it)
        if rect is None:
            continue
        d = dist_to_rect(rect, x, y)
        if d > max_dist:
            continue
        cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
        cands.append((d, ref, it, _direction(cx - x, cy - y)))
    cands.sort(key=lambda t: t[0])
    return [(ref, it, direction) for _d, ref, it, direction in cands[:k]]


def hit_summary(hit: dict | None) -> tuple[str, str]:
    """点击点下元素的描述：返回 (类别, 文本)。类别 interactive / text / blank / none。"""
    if not isinstance(hit, dict):
        return "none", ""
    ct = str(hit.get("ct") or "")
    name = str(hit.get("n") or "").strip()
    ix = bool(hit.get("ix"))
    if ix:
        label = CT_LABEL.get(ct) or ct or "控件"
        return "interactive", f"{label}「{name or '(无名称)'}」"
    if not name and ct in _BLANK_CT:
        return "blank", "没有可识别的控件（空白处、容器或自绘区域）"
    label = CT_LABEL.get(ct) or ct or "元素"
    return "text", f"{label}「{name or '(无名称)'}」（不是可点击控件，点到这里通常不会有反应）"


def hit_conflicts(hit: dict | None, target: dict | None) -> bool:
    """按编号点击时，点击点下的元素与目标元素是否明显不是同一个（多半被弹层 / 浮窗遮住）。"""
    if not isinstance(hit, dict) or not isinstance(target, dict):
        return False
    hn, tn = norm_text(str(hit.get("n") or "")), norm_text(label_of(target))
    if not hn or not tn:
        return False
    if hn in tn or tn in hn:
        return False
    hr, tr = hit.get("r"), element_rect(target)
    if isinstance(hr, (list, tuple)) and len(hr) >= 4 and tr is not None:
        hrect = (int(hr[0]), int(hr[1]), int(hr[2]), int(hr[3]))
        # 命中元素在目标内部（链接里的文字、按钮里的图标）算同一处；
        # 命中元素反而更大且名称不同，才是「有东西盖在上面」的典型形态
        if _center_inside(hrect, tr):
            return False
    return True
