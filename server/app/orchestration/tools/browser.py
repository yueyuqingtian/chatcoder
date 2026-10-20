"""浏览器工具套件（plan-20261015-browser-agent）。

工具直接操作右侧面板里的**内置浏览器**（用户看到的就是 AI 操作的页面）；没有桌面端连接时
自动回退到服务端后台浏览器。动作编排与文案在 `orchestration/browser/driver.py`，本文件只声明
工具 schema 并把调用转给 driver。

保留 0.8.1 的 6 个工具名（browser_navigate / screenshot / click / type / snapshot / evaluate）
与旧参数（selector、press_enter、clear_before、wait_until、url）——用户的执行策略规则、
自定义模式白名单与 TA3 别名都按名字引用它们。
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.orchestration.tools.base import Tool, ToolContext, ToolResult


def _check_browser_enabled() -> str | None:
    """浏览器工具门禁。

    plan-334-1661：浏览器能力有两个入口，任一开启即可用——
      1.「设置 → 常规 → 内置浏览器工具」（browser_enabled，历史入口）；
      2.「设置 → 电脑操控 → 浏览器操作」（desktop_enabled + desktop_browser_ops_enabled）。
    这样用户在专用页里把「电脑操控」打开后，不必再回到常规页二次开启同一件事；
    两个开关是「并集」而非「主从」，任一为真都放行。
    """
    if getattr(settings, "browser_enabled", False):
        return None
    if getattr(settings, "desktop_enabled", False) and getattr(
        settings, "desktop_browser_ops_enabled", True
    ):
        return None
    return (
        "浏览器工具当前未启用。可在「设置 → 常规 → 内置浏览器工具」中开启，"
        "或在「设置 → 电脑操控 → 浏览器操作」开启。"
    )


def browser_tools_enabled() -> bool:
    return _check_browser_enabled() is None


# ── 参数片段 ─────────────────────────────────────────────
_REF = {"type": "string", "description": "元素 ref（来自 browser_snapshot，如 ref_12）——首选的定位方式"}
_SELECTOR = {"type": "string", "description": "CSS 选择器（没有 ref 时使用）"}
_TEXT_TARGET = {"type": "string", "description": "按可见文字定位元素（如「登录」）"}
_X = {"type": "number", "description": "横坐标：最近一次视口截图上的像素位置"}
_Y = {"type": "number", "description": "纵坐标：最近一次视口截图上的像素位置"}
_TAB = {"type": "string", "description": "标签 id（缺省为 AI 当前标签）"}
_SHOT = {"type": "boolean", "description": "动作完成的当下附带视口截图（省去再调 browser_screenshot；出现报错时会自动附图）"}
_ON_DIALOG = {"type": "string", "enum": ["accept", "dismiss"], "description": "动作触发网页确认框时确认还是取消，默认 accept"}


def _schema(name: str, desc: str, props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, **({"required": required} if required else {})},
        },
    }


class _BrowserTool(Tool):
    action: str = ""
    risk_level = "low"
    # 功能开关：关闭时注册表不下发本工具 schema（见 ToolRegistry.for_agent）
    feature_enabled = staticmethod(browser_tools_enabled)

    def _desc(self) -> tuple[str, dict, list[str] | None]:  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def function_schema(self) -> dict:
        desc, props, required = self._desc()
        return _schema(self.name, desc, props, required)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)
        from app.orchestration.browser.driver import execute

        out = await execute(ctx.session_id, self.action, dict(args or {}), ctx.cancel_event)
        data = dict(out.data or {})
        if out.image_b64:
            # agent_loop 只认 data.base64 / data.filename（据扩展名推断 mime）把图片送进多模态对话
            data["base64"] = out.image_b64
            data["filename"] = "browser-shot.png" if "png" in out.image_mime else "browser-shot.jpg"
            data["mime_type"] = out.image_mime
        if not out.ok:
            return ToolResult(ok=False, output=out.text, error=out.error or "浏览器操作失败", data=data)
        text = out.text
        if self.action == "navigate":
            # plan-20261018-operator-boost：打开站点即附上该站点已沉淀的操作路线（同会话同条只附一次）
            from app.orchestration.route_hints import host_of, route_hint

            host = host_of(str(data.get("url") or ""))
            hint = await route_hint(ctx, scope="browser", names=[host], target_label=host) if host else ""
            if hint:
                text += hint
                data["route_hint"] = True
        return ToolResult(ok=True, output=text, data=data)


class BrowserNavigateTool(_BrowserTool):
    name = "browser_navigate"
    action = "navigate"
    risk_level = "high"
    description = "在右侧面板的内置浏览器中打开网址（用户可实时看到），或后退/前进/刷新。"

    def _desc(self):
        return (
            "在内置浏览器（右侧面板，用户可实时看到）打开网址，或 action=back/forward/reload。"
            "返回标题、地址、正文摘要与视口内的可交互元素（带 ref，可直接点击）；页面 DOM 就绪且接口安静即返回。"
            "localhost:5173 这类本地地址会自动补 http://。"
            "默认在 AI 自己的标签里打开，不会覆盖用户正在看的页面。",
            {
                "url": {"type": "string", "description": "目标网址"},
                "action": {"type": "string", "enum": ["back", "forward", "reload"], "description": "不传 url 时的导航动作"},
                "new_tab": {"type": "boolean", "description": "在新标签中打开"},
                "tab_id": _TAB,
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "commit"], "description": "等待条件，默认 domcontentloaded（load 等图片等全部加载完）"},
            },
            None,
        )


class BrowserTabsTool(_BrowserTool):
    name = "browser_tabs"
    action = "tabs"
    description = "列出、新建、切换、关闭内置浏览器的标签。"

    def _desc(self):
        return (
            "管理内置浏览器标签：list 列出（含 AI/用户标签与当前页面）、new 新建（可带 url）、"
            "select 切换后续动作操作的标签、close 关闭。",
            {
                "action": {"type": "string", "enum": ["list", "new", "select", "close"], "description": "默认 list"},
                "tab_id": _TAB,
                "url": {"type": "string", "description": "new 时打开的网址"},
            },
            None,
        )


class BrowserSnapshotTool(_BrowserTool):
    name = "browser_snapshot"
    action = "snapshot"
    risk_level = "low"
    description = "读取当前页面的可交互元素树，每个元素带 ref，供点击/输入精确定位。"

    def _desc(self):
        return (
            "读取当前页面结构：按钮、链接、输入框等可交互元素（带 ref，如 ref_12）与标题/分区。"
            "同源 iframe 的内容会按「框架「名称」」分组一并列出；跨域 iframe 的内容无法读取，需截图后用坐标操作。"
            "后续 browser_click / browser_type 等直接传 ref，最快最准；页面变化或跳转后 ref 会失效，需要重新读取。"
            "读页面优先用它或 browser_get_text，只在需要看视觉效果时截图。",
            {
                "query": {"type": "string", "description": "关键词筛选（按名称/角色/状态匹配），相当于在页面里查找元素"},
                "filter": {"type": "string", "enum": ["interactive", "all"], "description": "interactive（默认）只列可交互元素；all 同时列出文本"},
                "ref": {"type": "string", "description": "只读取某个元素的子树"},
                "viewport_only": {"type": "boolean", "description": "只列当前可视区域内的元素"},
                "max_chars": {"type": "integer", "description": "输出字符上限，默认 12000"},
                "tab_id": _TAB,
            },
            None,
        )


class BrowserGetTextTool(_BrowserTool):
    name = "browser_get_text"
    action = "get_text"
    description = "读取页面（或某个元素）的正文文本。"

    def _desc(self):
        return (
            "读取页面正文（自动选主体区域，去掉导航等杂项），或用 ref / selector 读取某个元素内的文字。",
            {"ref": _REF, "selector": _SELECTOR, "max_chars": {"type": "integer", "description": "字符上限，默认 8000"}},
            None,
        )


class BrowserScreenshotTool(_BrowserTool):
    name = "browser_screenshot"
    action = "screenshot"
    risk_level = "low"
    description = "截取内置浏览器当前页面（视口 / 整页 / 单个元素）。"

    def _desc(self):
        return (
            "截取当前页面：默认视口截图（图上像素坐标可直接用于 browser_click 的 x/y），"
            "full_page=true 截整页（图上坐标为整页位置，点击时自动滚到该处），ref / selector 截单个元素。"
            "marks=true 在图上给可交互元素画编号框并列出编号对应的 ref，看图定位后按 ref 点击最准。"
            "面板隐藏时也能截。读文字内容请优先用 browser_get_text。",
            {
                "full_page": {"type": "boolean", "description": "截取整页长图"},
                "marks": {"type": "boolean", "description": "在截图上标出可交互元素的编号"},
                "ref": _REF,
                "selector": _SELECTOR,
                "url": {"type": "string", "description": "（兼容旧参数）先打开该网址再截图"},
            },
            None,
        )


class BrowserClickTool(_BrowserTool):
    name = "browser_click"
    action = "click"
    risk_level = "high"
    description = "点击页面元素（ref / 选择器 / 文字 / 坐标），支持右键、双击与修饰键。"

    def _desc(self):
        return (
            "点击页面元素：优先用 ref；也可用 selector、text（可见文字）或截图坐标 x/y。"
            "目标不在视口时会自动滚入；被弹窗遮挡或禁用时会报错说明；text 匹配到多个同名元素时报错并列出候选 ref。"
            "点击后等页面接口返回再报告：跳转/变化、报错提示（即使一闪而过）、接口错误、新弹出的菜单或对话框里的元素（带 ref）；"
            "出现报错时自动附截图。",
            {
                "ref": _REF, "selector": _SELECTOR, "text": _TEXT_TARGET, "x": _X, "y": _Y,
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "默认 left"},
                "click_count": {"type": "integer", "description": "2 为双击，3 为三击"},
                "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Control", "Shift", "Alt", "Meta"]}, "description": "按住的修饰键"},
                "force": {"type": "boolean", "description": "目标被遮挡/禁用时仍然点击"},
                "on_dialog": _ON_DIALOG,
                "screenshot": _SHOT,
            },
            None,
        )


class BrowserTypeTool(_BrowserTool):
    name = "browser_type"
    action = "type_text"
    risk_level = "high"
    description = "向输入框输入文字（默认先清空），可回车提交。"

    def _desc(self):
        return (
            "向输入框 / 文本域 / 富文本编辑区输入文字。用 ref / selector / text 指定目标，不指定则输入到当前焦点。"
            "默认先清空原内容（clear=false 追加）；submit=true 输入后按 Enter。支持中文。",
            {
                "text": {"type": "string", "description": "要输入的文字"},
                "ref": _REF, "selector": _SELECTOR,
                "clear": {"type": "boolean", "description": "输入前清空，默认 true"},
                "submit": {"type": "boolean", "description": "输入后按 Enter"},
                "press_enter": {"type": "boolean", "description": "同 submit（兼容旧参数）"},
                "screenshot": _SHOT,
                "clear_before": {"type": "boolean", "description": "同 clear（兼容旧参数）"},
            },
            ["text"],
        )


class BrowserPressTool(_BrowserTool):
    name = "browser_press"
    action = "press"
    risk_level = "high"
    description = "按键或组合键（Enter、Tab、Escape、Control+A…）。"

    def _desc(self):
        return (
            "在页面中按键：单键（Enter、Tab、Escape、Backspace、ArrowDown…）或组合键（Control+A、Shift+Tab）；"
            "空格分隔表示依次按下（\"Tab Tab Enter\"）。可先用 ref 聚焦目标。",
            {
                "keys": {"type": "string", "description": "按键，如 \"Enter\"、\"Control+A\"、\"Tab Tab Enter\""},
                "repeat": {"type": "integer", "description": "重复次数，默认 1"},
                "ref": _REF,
                "on_dialog": _ON_DIALOG,
                "screenshot": _SHOT,
            },
            ["keys"],
        )


class BrowserScrollTool(_BrowserTool):
    name = "browser_scroll"
    action = "scroll"
    description = "滚动页面或某个滚动区域，或把元素滚入视口。"

    def _desc(self):
        return (
            "滚动：direction=up/down/left/right（amount 为像素，或 ≤10 的数字表示屏数，默认 0.8 屏）；"
            "带 ref 且不带 direction 时把该元素滚入视口中央；带 ref 和 direction 时滚动该元素所在的区域；"
            "to=top/bottom 直达顶部或底部。",
            {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "number", "description": "像素，或 ≤10 表示屏数"},
                "ref": _REF, "selector": _SELECTOR, "text": _TEXT_TARGET,
                "to": {"type": "string", "enum": ["top", "bottom"]},
            },
            None,
        )


class BrowserHoverTool(_BrowserTool):
    name = "browser_hover"
    action = "hover"
    description = "把鼠标悬停到元素上（展开悬停菜单、查看提示）。"

    def _desc(self):
        return (
            "把鼠标悬停到元素上，用于展开悬停菜单、显示提示等。",
            {"ref": _REF, "selector": _SELECTOR, "text": _TEXT_TARGET, "x": _X, "y": _Y, "screenshot": _SHOT},
            None,
        )


class BrowserDragTool(_BrowserTool):
    name = "browser_drag"
    action = "drag"
    risk_level = "high"
    description = "拖拽：从一个元素/坐标拖到另一个元素/坐标。"

    def _desc(self):
        return (
            "拖拽：起点用 from_ref / from_selector / from_x+from_y，终点用 to_ref / to_selector / to_x+to_y。",
            {
                "from_ref": _REF, "from_selector": _SELECTOR, "from_x": _X, "from_y": _Y,
                "to_ref": _REF, "to_selector": _SELECTOR, "to_x": _X, "to_y": _Y,
            },
            None,
        )


class BrowserFillTool(_BrowserTool):
    name = "browser_fill"
    action = "fill"
    risk_level = "high"
    description = "直接设置表单控件的值：下拉框选项、复选/单选/开关、输入框（可一次填多项）。"

    def _desc(self):
        return (
            "直接设置表单控件的值并触发 input/change：下拉框按选项文字或值选择；复选框/开关传 true/false；"
            "输入框整体替换。用 fields 一次填写多项：[{\"ref\":\"ref_3\",\"value\":\"张三\"}, …]。",
            {
                "fields": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"ref": {"type": "string"}, "selector": {"type": "string"}, "value": {}}},
                    "description": "多项填写",
                },
                "ref": _REF, "selector": _SELECTOR,
                "value": {"description": "单项填写的值"},
            },
            None,
        )


class BrowserClipboardTool(_BrowserTool):
    name = "browser_clipboard"
    action = "clipboard"
    risk_level = "high"
    description = "页面内复制、剪切、粘贴、全选（默认用 AI 剪贴板，不影响用户的系统剪贴板）。"

    def _desc(self):
        return (
            "复制 / 剪切 / 粘贴 / 全选：copy 复制当前选中内容（或 ref 指定元素的全部文字），cut 剪切，"
            "paste 把剪贴板（或 text 参数）粘贴到焦点或 ref 指定的输入框，select_all 全选。"
            "默认使用 AI 自己的剪贴板；system=true 才读写用户的系统剪贴板。",
            {
                "action": {"type": "string", "enum": ["copy", "cut", "paste", "select_all"]},
                "ref": _REF, "selector": _SELECTOR,
                "text": {"type": "string", "description": "paste 时直接粘贴这段文字"},
                "system": {"type": "boolean", "description": "使用系统剪贴板"},
            },
            ["action"],
        )


class BrowserWaitTool(_BrowserTool):
    name = "browser_wait"
    action = "wait"
    description = "等待文字/元素出现或消失、页面加载完成，或固定时长。"

    def _desc(self):
        return (
            "等待条件满足：text 文字出现、selector / ref 元素出现（gone=true 等它消失）；都不传则等页面加载完成；"
            "只传 ms 为固定等待（上限 30 秒）。",
            {
                "text": {"type": "string"}, "selector": _SELECTOR, "ref": _REF,
                "gone": {"type": "boolean"},
                "timeout_ms": {"type": "integer", "description": "默认 10000"},
                "ms": {"type": "integer", "description": "固定等待毫秒数"},
            },
            None,
        )


class BrowserEvaluateTool(_BrowserTool):
    name = "browser_evaluate"
    action = "evaluate"
    risk_level = "high"
    description = "在当前页面执行 JavaScript 并返回结果。"

    def _desc(self):
        return (
            "在当前页面上下文执行 JavaScript 并返回结果。脚本可以是表达式、多条语句（返回最后一个表达式的值），"
            "也可以直接用顶层 return / await——无需包成函数；返回值需可序列化（对象请返回普通数据）。"
            "运行时出错会带回真实的错误类型与信息。常规点击 / 输入请用专用工具，它们更快且对用户可见。",
            {"script": {"type": "string", "description": "JavaScript 代码，如 `document.title` 或 `const n = document.querySelectorAll('li').length; return n;`"}},
            ["script"],
        )


class BrowserLogsTool(_BrowserTool):
    name = "browser_logs"
    action = "logs"
    description = "读取页面控制台消息或网络请求（含响应体）。"

    def _desc(self):
        return (
            "读取当前标签的控制台消息（kind=console）或网络请求（kind=network），可按 pattern 过滤、only_errors 只看错误；"
            "传 request_id 读取某个请求的响应体。用于调试前端报错与接口问题。",
            {
                "kind": {"type": "string", "enum": ["console", "network"], "description": "默认 console"},
                "only_errors": {"type": "boolean"},
                "pattern": {"type": "string", "description": "关键词过滤"},
                "limit": {"type": "integer", "description": "默认 50"},
                "request_id": {"type": "string", "description": "读取该请求的响应体"},
                "clear": {"type": "boolean", "description": "读取后清空"},
            },
            None,
        )


_BATCH_ACTION_NAMES = ["navigate", "click", "type", "fill", "press", "scroll", "hover", "drag", "wait",
                       "clipboard", "screenshot", "snapshot", "get_text", "evaluate", "tabs"]


class BrowserBatchTool(_BrowserTool):
    name = "browser_batch"
    action = "batch"
    risk_level = "high"
    description = "一次按顺序执行多个浏览器动作（任一步失败即停止）。"

    def _desc(self):
        return (
            "一次执行多步浏览器动作，省去逐步往返：每步是 {\"action\": 动作名, ...该动作的参数}，"
            "动作名为 navigate / click / type / fill / press / scroll / hover / drag / wait / clipboard / "
            "screenshot / snapshot / get_text / evaluate / tabs；任一步失败立即停止并说明。"
            "流程明确时（如填表并提交）优先用它；then=screenshot/snapshot 在末尾附带截图或页面结构。",
            {
                "actions": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"action": {"type": "string", "enum": _BATCH_ACTION_NAMES}}, "required": ["action"]},
                    "description": "如 [{\"action\":\"type\",\"ref\":\"ref_3\",\"text\":\"hi\"},{\"action\":\"click\",\"ref\":\"ref_5\"}]",
                },
                "then": {"type": "string", "enum": ["screenshot", "snapshot"]},
                "screenshot": {"type": "boolean", "description": "同 then=screenshot：全部完成后附带截图"},
            },
            ["actions"],
        )


class BrowserDownloadsTool(_BrowserTool):
    name = "browser_downloads"
    action = "downloads"
    description = "查看内置浏览器的下载记录（保存路径、进度），可等待下载完成。"

    def _desc(self):
        return (
            "列出内置浏览器最近的下载：保存路径（设置里的「浏览器下载目录」）、状态与大小。"
            "导出 / 下载类操作后用它拿到文件的完整路径；下载未完成时传 wait_ms 等待完成。"
            "拿到路径后按需核对内容：fs_read（文本）、terminal_exec（脚本校验表格 / 数据）、电脑操控打开关联软件看排版。",
            {
                "wait_ms": {"type": "integer", "description": "等待进行中的下载完成的最长毫秒数（默认 0 = 不等），最多 120000"},
                "limit": {"type": "integer", "description": "最多列出多少条，默认 10"},
            },
            None,
        )


class BrowserRecipeTool(Tool):
    """网页操作路线（plan-20261018-operator-boost）：与电脑操控共用路线表，scope=browser，按站点 host 存。"""

    name = "browser_recipe"
    risk_level = "low"
    feature_enabled = staticmethod(browser_tools_enabled)
    description = (
        "记录或查询内置浏览器的「操作路线」——某站点某操作意图的通用做法，用于下次直接复用、少走弯路。\n"
        "action=recall：在某站点做不熟悉的操作前查一次（browser_navigate 打开站点时也会自动附上已有路线）。\n"
        "action=save：一次多步网页操作成功后沉淀：site 填站点 host（如 localhost:5173、erp.example.com），"
        "intent 一句话目标，principle 写怎么定位元素、按什么顺序、哪些步骤可合并，pitfalls 写踩过的坑。\n"
        "**存通用做法，不存 ref / 坐标**——它们每次都会变。同站点同意图只保留一条，重复保存会更新内容。"
    )

    def function_schema(self) -> dict:
        return _schema(self.name, self.description, {
            "action": {"type": "string", "enum": ["recall", "save"], "description": "recall（查询）或 save（保存）"},
            "site": {"type": "string", "description": "站点 host（如 localhost:5173）；recall 时可省略，默认当前页面"},
            "intent": {"type": "string", "description": "操作意图，一句话（如「导出本月订单为 Excel」）"},
            "principle": {"type": "string", "description": "action=save 时必填：通用做法"},
            "pitfalls": {"type": "string", "description": "action=save 时可选：踩过的坑"},
        }, ["action"])

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = _check_browser_enabled()
        if err:
            return ToolResult(ok=False, output="", error=err)
        if not getattr(settings, "desktop_recipe_enabled", True):
            return ToolResult(ok=False, output="", error="操作路线功能已在「设置 → 电脑操控」中关闭。")
        if ctx.db is None:
            return ToolResult(ok=False, output="", error="当前上下文没有数据库会话，无法读写操作路线。")
        from app.orchestration.browser.driver import current_url
        from app.orchestration.route_hints import host_of, note_recalled
        from app.services import desktop_recipe_service as svc

        action = str(args.get("action") or "").lower()
        site = str(args.get("site") or "").strip()
        site = host_of(site) if ("/" in site or "://" in site) else site.lower()
        if not site:
            site = host_of(current_url(ctx.session_id))
        intent = str(args.get("intent") or "")

        if action == "recall":
            if not site and not intent:
                return ToolResult(ok=False, output="", error="至少提供 site 或 intent 之一（或先打开目标站点）。")
            hits = []
            if site:
                hits = await svc.match_for_target(ctx.db, scope=svc.SCOPE_BROWSER, names=[site], limit=5)
                if intent:
                    low = intent.casefold()
                    hits.sort(key=lambda h: 0 if (low in (h.intent or "").casefold() or (h.intent or "").casefold() in low) else 1)
                # match_for_target 不计数（它也服务于自动附带）；显式查询命中才算一次复用
                for h in hits[:3]:
                    h.usage_count = (h.usage_count or 0) + 1
            else:
                hits = await svc.recall_recipe(ctx.db, intent=intent, limit=3, scope=svc.SCOPE_BROWSER)
            if not hits:
                return ToolResult(ok=True, output=(
                    f"没有找到「{site or intent}」的网页操作路线。按常规方式操作；"
                    "成功完成多步操作后用 action=save 沉淀，下次同类操作即可直接复用。"
                ), data={"count": 0})
            await ctx.db.commit()
            note_recalled(ctx.session_id, [h.id for h in hits])
            lines = [f"找到 {len(hits)} 条网页操作路线，按做法直接执行："]
            for h in hits[:3]:
                lines.append(f"\n【{h.app_name} / {h.intent}】（已复用 {h.usage_count} 次）")
                lines.append(f"  做法：{h.principle}")
                if h.pitfalls:
                    lines.append(f"  注意：{h.pitfalls}")
            return ToolResult(ok=True, output="\n".join(lines), data={"count": len(hits)})

        if action == "save":
            try:
                row, is_new = await svc.save_recipe(
                    ctx.db, app_name=site, intent=intent,
                    principle=str(args.get("principle") or ""), pitfalls=str(args.get("pitfalls") or ""),
                    scope=svc.SCOPE_BROWSER,
                )
            except ValueError as exc:
                msg = str(exc).replace("app_name", "site（站点 host）")
                return ToolResult(ok=False, output="", error=msg)
            # 工具专用会话：必须 commit，否则工具结束即回滚（同 desktop_recipe）
            await ctx.db.commit()
            verb = "已记录" if is_new else "已更新"
            return ToolResult(ok=True, output=f"{verb}网页操作路线「{row.app_name} / {row.intent}」。",
                              data={"id": row.id, "created": is_new})

        return ToolResult(ok=False, output="", error="action 必须是 recall 或 save。")


ALL_BROWSER_TOOLS = (
    BrowserNavigateTool, BrowserTabsTool, BrowserSnapshotTool, BrowserGetTextTool, BrowserScreenshotTool,
    BrowserClickTool, BrowserTypeTool, BrowserPressTool, BrowserScrollTool, BrowserHoverTool,
    BrowserDragTool, BrowserFillTool, BrowserClipboardTool, BrowserWaitTool, BrowserEvaluateTool,
    BrowserLogsTool, BrowserBatchTool, BrowserDownloadsTool, BrowserRecipeTool,
)

BROWSER_TOOL_NAMES = frozenset(cls.name for cls in ALL_BROWSER_TOOLS)
