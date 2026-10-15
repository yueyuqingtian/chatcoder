"""按键组合规范化（plan-20261015-browser-agent）。

模型写法五花八门（`ctrl+a`、`Control+A`、`cmd+c`、`Tab Tab Enter`、`["Tab","Enter"]`），
这里统一成 W3C KeyboardEvent.key 风格的组合串（修饰键在前、主键在后，`+` 连接），
两种宿主各自再映射到 Electron keyCode / Playwright 键名——Playwright 直接认这种写法。
"""
from __future__ import annotations

_MODS = {
    "ctrl": "Control", "control": "Control", "ctl": "Control",
    "shift": "Shift",
    "alt": "Alt", "option": "Alt", "opt": "Alt",
    "meta": "Meta", "cmd": "Meta", "command": "Meta", "win": "Meta", "super": "Meta",
}
_MOD_ORDER = ["Control", "Alt", "Shift", "Meta"]

_KEYS = {
    "enter": "Enter", "return": "Enter",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab",
    "space": " ", "spacebar": " ",
    "backspace": "Backspace", "bksp": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown", "pgdown": "PageDown",
    "up": "ArrowUp", "arrowup": "ArrowUp",
    "down": "ArrowDown", "arrowdown": "ArrowDown",
    "left": "ArrowLeft", "arrowleft": "ArrowLeft",
    "right": "ArrowRight", "arrowright": "ArrowRight",
    "plus": "+", "minus": "-",
}
for _i in range(1, 13):
    _KEYS[f"f{_i}"] = f"F{_i}"


class KeyParseError(ValueError):
    pass


def _norm_combo(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise KeyParseError("按键不能为空")
    # 单个 "+" 键本身
    if text == "+":
        return "+"
    parts = [p for p in text.replace(" + ", "+").split("+")]
    # "Control++" 这类以加号为主键的写法
    if text.endswith("++"):
        parts = parts[:-2] + ["+"]
    mods: list[str] = []
    key = ""
    for i, p in enumerate(parts):
        token = p.strip()
        if not token:
            continue
        low = token.lower()
        if low in _MODS and i < len(parts) - 1:
            m = _MODS[low]
            if m not in mods:
                mods.append(m)
            continue
        if low in _MODS and i == len(parts) - 1 and not key:
            # 只按修饰键本身（如 "Shift"）
            key = _MODS[low]
            continue
        key = _KEYS.get(low, token if len(token) == 1 else token[:1].upper() + token[1:])
    if not key:
        raise KeyParseError(f"无法识别的按键：{raw}")
    if len(key) == 1 and key.isalpha() and any(m in mods for m in ("Control", "Alt", "Meta")):
        key = key.upper()
    ordered = [m for m in _MOD_ORDER if m in mods]
    return "+".join(ordered + [key])


def normalize_keys(keys) -> list[str]:
    """把模型给的按键参数规范化为组合串列表（按顺序依次按下）。"""
    seq: list[str] = []
    if isinstance(keys, (list, tuple)):
        items = [str(k) for k in keys]
    else:
        s = str(keys or "").strip()
        if not s:
            raise KeyParseError("keys 不能为空")
        # 空格分隔表示依次按下（"Tab Tab Enter"、"Control+A Delete"）；"ctrl + a" 这种带空格的
        # 组合先并拢；单独的空格键写 "Space"
        while " +" in s or "+ " in s:
            s = s.replace(" +", "+").replace("+ ", "+")
        items = s.split()
    for item in items:
        seq.append(_norm_combo(item))
    if len(seq) > 50:
        raise KeyParseError("一次最多按 50 个键")
    return seq


def describe(combo: str) -> str:
    """展示用文案（状态条 / 结果）：Control+A → Ctrl+A，空格显示为 Space。"""
    return combo.replace("Control", "Ctrl").replace("Meta", "Win").replace(" ", "Space") if combo != " " else "Space"
