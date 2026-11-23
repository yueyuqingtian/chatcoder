"""antigravity 模型目录：内置兜底清单 + fetchAvailableModels 解析（方案 §2.3）。

两个职责：

1. `BUILTIN_MODELS`：离线兜底。本机实测 fetchAvailableModels 返回 22 项，其中
   2 个为代码补全专用（tab_*），1 个为图像生成；对话可用的 20 个落在这里。
2. `parse_available_models()`：把网关响应解析成 {chat, image, completion, all}，
   按响应自身的 imageGenerationModelIds 等字段分类，而不是靠名字猜——新模型上线
   时分类自动正确。
"""
from __future__ import annotations

# 对话可用模型（离线兜底，非权威；同步成功后以远端清单为准）
BUILTIN_MODELS: list[str] = [
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-thinking",
    "gemini-2.5-pro",
    "gemini-3-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-tiered",
    "gemini-4-hydrogen-sonar",
    "gpt-oss-120b-medium",
]

# 补全专用（出现在 models 里但不能当对话模型用）
_COMPLETION_PREFIXES = ("tab_",)

# 对话模型的展示名（未列出的按 id 原样展示）
_DISPLAY_NAMES: dict[str, str] = {
    "claude-opus-5-5-low": "Claude Opus 5.5 Low",
    "claude-opus-5-5-medium": "Claude Opus 5.5",
    "claude-opus-5-5-high": "Claude Opus 5.5 High",
    "claude-sonnet-5-5-low": "Claude Sonnet 5.5 Low",
    "claude-sonnet-5-5-medium": "Claude Sonnet 5.5",
    "claude-sonnet-5-5-high": "Claude Sonnet 5.5 High",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-2.5-flash-thinking": "Gemini 2.5 Flash Thinking",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-3-flash": "Gemini 3 Flash",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash Low",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash High",
    "gemini-3.8-flash-tiered": "Gemini 3.8 Flash Tiered",
    "gemini-4-hydrogen-sonar": "Gemini 4 Hydrogen",
    "gpt-oss-120b-medium": "GPT-OSS 120B",
}


def display_name(model_id: str) -> str:
    return _DISPLAY_NAMES.get(model_id, model_id)


# ---- 模型家族（family）合并 ------------------------------------------------------
#
# Antigravity 把同一模型的多个思考档位拆成多个 id（gemini-3.8-flash-low / -medium /
# -high），界面上却是一个模型加一个档位选择器。这里提供拆分工具，供上层把变体合并成
# 「家族 + 档位列表」，按档位再选回真实 id 发请求。
#
# 档位词长短不一，必须**最长优先**匹配，否则 "extra-low" 会被 "low" 抢先吃掉。

_EFFORT_WORDS: tuple[tuple[str, str], ...] = (
    ("extra-high", "xhigh"), ("extra-low", "minimal"), ("xhigh", "xhigh"),
    ("minimal", "minimal"), ("low", "low"), ("medium", "medium"), ("high", "high"),
)

_EFFORT_RANK = ("minimal", "low", "medium", "high", "xhigh")


def split_family(model_id: str) -> tuple[str, str]:
    """把变体 id 拆成 (家族 id, 档位)。

    "gemini-3.8-flash-medium" → ("gemini-3.8-flash", "medium")；
    没有档位词时返回 (id, "")。
    """
    text = str(model_id or "")
    for word, level in _EFFORT_WORDS:
        suffix = "-" + word
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)], level
    return text, ""


def family_of(model_id: str) -> str:
    """变体 id 所属的家族 id（无档位词时是它自己）。"""
    return split_family(model_id)[0]


def effort_rank(level: str) -> int:
    """档位强弱排序（未知档位排在最后，便于稳定输出）。"""
    try:
        return _EFFORT_RANK.index(level)
    except ValueError:
        return len(_EFFORT_RANK)


def variants_by_effort(model_ids) -> dict[str, str]:
    """同一家族的「档位 → 真实 id」映射，"" 为不指定档位时的默认。

    默认优先取家族本身的裸 id；没有裸 id 时退到 high（Gemini 未指定档位时的实际
    行为），再退到最强的可用档位。
    """
    mapping: dict[str, str] = {}
    for mid in model_ids:
        _, level = split_family(mid)
        mapping.setdefault(level, mid)
    if "" not in mapping:
        default = mapping.get("high")
        if default is None:
            for level in reversed(_EFFORT_RANK):
                if level in mapping:
                    default = mapping[level]
                    break
        if default is not None:
            mapping[""] = default
    return mapping


def _is_completion_model(model_id: str) -> bool:
    return model_id.startswith(_COMPLETION_PREFIXES)


def parse_available_models(payload: dict | None) -> dict[str, list[str]]:
    """解析 fetchAvailableModels 响应。

    返回 {"chat": [...], "image": [...], "completion": [...], "all": [...]}，
    全部按 ASCII 排序保证界面顺序稳定。分类依据是响应自带的分类字段，
    缺字段时用 tab_ 前缀兜底。
    """
    payload = payload if isinstance(payload, dict) else {}
    models = payload.get("models")
    all_ids: list[str] = []
    if isinstance(models, dict):
        all_ids = [str(k) for k in models.keys()]
    elif isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                mid = item.get("modelId") or item.get("name") or item.get("id")
                if mid:
                    all_ids.append(str(mid))
            elif isinstance(item, str):
                all_ids.append(item)

    def _ids(key: str) -> set[str]:
        raw = payload.get(key)
        if not isinstance(raw, list):
            return set()
        out: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                mid = item.get("modelId") or item.get("name") or item.get("id")
                if mid:
                    out.add(str(mid))
        return out

    image_ids = _ids("imageGenerationModelIds")
    # 补全/跳转/查询类专用模型都不该进对话下拉
    exclusive: set[str] = set()
    for key in (
        "commandModelIds",
        "mqueryModelIds",
        "webSearchModelIds",
        "commitMessageModelIds",
        "audioTranscriptionModelIds",
    ):
        exclusive |= _ids(key)

    chat: list[str] = []
    image: list[str] = []
    completion: list[str] = []
    for mid in dict.fromkeys(all_ids):
        if mid in image_ids:
            image.append(mid)
        elif _is_completion_model(mid):
            completion.append(mid)
        elif mid in exclusive:
            completion.append(mid)
        else:
            chat.append(mid)

    return {
        "chat": sorted(chat),
        "image": sorted(image),
        "completion": sorted(completion),
        "all": sorted(dict.fromkeys(all_ids)),
    }


def catalog_model_rows(parsed: dict[str, list[str]]) -> list[dict]:
    """把解析结果转成落库用的模型行（供 provider_service 同步复用）。"""
    rows: list[dict] = []
    for mid in parsed.get("chat") or []:
        rows.append({
            "name": mid,
            "display_name": display_name(mid),
            "kind": "chat",
        })
    for mid in parsed.get("image") or []:
        rows.append({
            "name": mid,
            "display_name": display_name(mid),
            "kind": "image",
        })
    return rows
