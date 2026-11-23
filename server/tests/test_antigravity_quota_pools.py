"""antigravity 额度池重建与模型家族拆分单测。

对应 plan-antigravity-provider-integration §11.9：池数量完全由数据决定，
不再假设「服务端只给两个组 = 界面上只有两个池」。全部是纯函数断言，
不发网络请求、不连数据库。
"""
import pytest

from app.auth.antigravity import catalog, quota

_FIVE_RESET = "2026-11-21T18:00:00Z"
_WEEKLY_RESET = "2026-11-28T08:00:00Z"


# ── 构造器：模拟 normalize_summary / normalize_models 的输出结构 ────────────────
def _bucket(window: str, remaining_percent, reset_time=None) -> dict:
    """单个 bucket，字段名与 normalize_summary 输出一致（百分比而非 fraction）。"""
    return {
        "bucket_id": f"{window or 'x'}-id",
        "display_name": "",
        "window": window,
        "reset_time": reset_time,
        "remaining_percent": remaining_percent,
    }


def _group(display_name: str, description: str = "", *,
           five_remaining=80.0, weekly_remaining=50.0,
           five_reset=_FIVE_RESET, weekly_reset=_WEEKLY_RESET) -> dict:
    """一个服务端声明的模型组，默认带 5h + weekly 两个桶。"""
    return {
        "display_name": display_name,
        "description": description,
        "buckets": [
            _bucket("5h", five_remaining, five_reset),
            _bucket("weekly", weekly_remaining, weekly_reset),
        ],
    }


def _window(name: str, model_id: str, used: float, reset_time=None) -> dict:
    """一个逐模型窗口，字段与 get_quota 组装的 model_windows 元素一致。"""
    return {
        "name": name,
        "model_id": model_id,
        "vendor": quota._vendor_of(model_id, name),
        "used_percent": used,
        "remaining_percent": round(100.0 - used, 1),
        "reset_time": reset_time,
    }


# ── build_pools：池数量（最核心的不变量）────────────────────────────────────
class TestPoolCount:
    def test_two_groups_two_buckets_make_four_pools(self):
        """2 组 × 2 桶必须展开成 4 个池窗口——池是 bucket 维度而不是组维度。"""
        summary = {"groups": [
            _group("Gemini Models", "Models within this group: Gemini 3.8 Flash"),
            _group("Claude and GPT models", "Models within this group: Claude, GPT-OSS"),
        ]}
        pools = quota.build_pools(summary, [])
        assert len(pools) == 4
        # 组名归一后才作为池名暴露给前端："Gemini Models" -> "Gemini"
        assert [p["pool"] for p in pools] == ["Gemini", "Gemini", "Claude & GPT", "Claude & GPT"]
        assert [p["name"] for p in pools] == ["5 小时", "7 天", "5 小时", "7 天"]
        assert [p["used_percent"] for p in pools] == [20.0, 50.0, 20.0, 50.0]
        # aside=True 表示这是服务端声明的组池（与本地造的独立池区分）
        assert all(p["aside"] is True for p in pools)
        # span_seconds 决定进度条的时间刻度，缺了前端只能靠名字猜窗口长度
        assert [p.get("span_seconds") for p in pools] == [5 * 3600, 7 * 24 * 3600] * 2

    def test_third_pool_created_when_reading_disagrees(self):
        """读数与所属组的所有桶都对不上 → 必须本地造出第三个池（用户实测的 Argon 场景）。"""
        summary = {"groups": [_group("Gemini Models", "Models within this group: Gemini")]}
        window = _window("Gemini 4 Argon", "gemini-4-argon", 10.0)
        pools = quota.build_pools(summary, [window])

        # 组只声明了两个桶，多出来的那个只能是本地造的
        assert len(pools) == 3
        created = pools[-1]
        assert created["pool"] == "Gemini 4 Argon"
        assert created["aside"] is True
        # 无 reset_time 视为 5 小时窗口，读数直接来自模型窗口
        assert created["name"] == "5 小时"
        assert created["used_percent"] == 10.0
        assert created["remaining_percent"] == 90.0
        # 模型窗口被原地回填到新池，否则前端分组时它会无处可去
        assert window["pool"] == "Gemini 4 Argon"

    def test_no_extra_pool_when_reading_matches(self):
        """读数与组 5h 桶一致 → 直接挂到组池，不允许凭空多造一个池。"""
        summary = {"groups": [_group("Gemini Models", "Models within this group: Gemini")]}
        window = _window("Gemini 4 Argon", "gemini-4-argon", 20.0, _FIVE_RESET)
        pools = quota.build_pools(summary, [window])

        assert len(pools) == 2
        assert [p["pool"] for p in pools] == ["Gemini", "Gemini"]
        assert window["pool"] == "Gemini"

    def test_no_extra_pool_when_same_reset_within_rpc_skew(self):
        """同重置时刻允许小幅 RPC 统计偏差（如 78.6% vs 79.4%），防止 25 个 Gemini 模型拆成 25 个独立池。"""
        summary = {"groups": [
            _group("Gemini Models", "Models within this group: Gemini",
                   five_remaining=78.6, weekly_remaining=75.3,
                   five_reset=_FIVE_RESET, weekly_reset=_WEEKLY_RESET),
        ]}
        flash = _window("Gemini 2.5 Flash", "gemini-2.5-flash", 20.6, _FIVE_RESET)
        argon = _window("Gemini 4 Argon", "gemini-4-argon-high", 0.0, "2026-11-29T08:00:00Z")
        pools = quota.build_pools(summary, [flash, argon])

        assert flash["pool"] == "Gemini"
        assert argon["pool"] == "Gemini 4 Argon"
        assert [p["pool"] for p in pools] == ["Gemini", "Gemini", "Gemini 4 Argon"]

    def test_no_extra_pool_when_both_resets_missing(self):
        """reset_time 双方都缺时只比百分比，仍算一致——缺字段不能当成不一致。"""
        summary = {"groups": [
            _group("Gemini Models", "Models within this group: Gemini", five_reset=None),
        ]}
        window = _window("Gemini 4 Argon", "gemini-4-argon", 20.0)
        pools = quota.build_pools(summary, [window])

        assert len(pools) == 2
        assert window["pool"] == "Gemini"

    def test_pool_count_follows_group_count(self):
        """3 个组 → 6 个池窗口：池数量跟数据走，不能硬编码「只有两个池」。"""
        summary = {"groups": [
            _group("Gemini Models", "Models within this group: Gemini"),
            _group("Claude and GPT models", "Models within this group: Claude, GPT-OSS"),
            _group("Gemini 4 Argon", "Models within this group: Gemini 4 Argon"),
        ]}
        pools = quota.build_pools(summary, [])

        assert len(pools) == 6
        # 第三组本身已被服务端声明，不能再被当成「额外造出来的池」
        assert [p["pool"] for p in pools].count("Gemini 4 Argon") == 2


# ── build_pools：挂池规则 ─────────────────────────────────────────────────
class TestPoolAttachment:
    def test_versioned_pool_matches_by_display_name(self):
        """版本化池（含数字）按名字前缀挂：Gemini 3 Pro 属于 Gemini 3 而不是泛化的 Gemini。"""
        summary = {"groups": [
            _group("Gemini 3 Models", "Models within this group: Gemini 3"),
        ]}
        window = _window("Gemini 3 Pro (High)", "gemini-3-pro-high", 42.0)
        pools = quota.build_pools(summary, [window])

        assert len(pools) == 2
        assert window["pool"] == "Gemini 3"

    def test_versioned_pool_matches_by_model_id(self):
        """显示名认不出时用 model_id（连字符转空格）兜底，避免真有模型挂不上池。"""
        summary = {"groups": [
            _group("Gemini 3 Models", "Models within this group: Gemini 3"),
        ]}
        window = _window("未收录的显示名", "gemini-3-pro-high", 42.0)
        pools = quota.build_pools(summary, [window])

        assert len(pools) == 2
        assert window["pool"] == "Gemini 3"

    def test_family_mismatch_not_attached(self):
        """claude 模型读数哪怕与 Gemini 池完全一致，也不能被挂进 Gemini 池。"""
        summary = {"groups": [_group("Gemini Models", "Models within this group: Gemini")]}
        window = _window("Claude Opus 5.5", "claude-opus-5-5-medium", 20.0, _FIVE_RESET)
        pools = quota.build_pools(summary, [window])

        # 只有组池，没有新增池，也没有被错误挂载
        assert len(pools) == 2
        assert not window.get("pool")

    def test_missing_fraction_counts_as_used_up(self):
        """服务端缺 remaining_percent 时按「已用尽」处理（100），不能当成 0 用量。"""
        summary = {"groups": [{
            "display_name": "Gemini Models",
            "description": "",
            "buckets": [_bucket("5h", None), _bucket("weekly", None)],
        }]}
        pools = quota.build_pools(summary, [])

        assert [p["used_percent"] for p in pools] == [100.0, 100.0]
        assert [p["remaining_percent"] for p in pools] == [None, None]


# ── 辅助函数：组名、版本化、读数一致、family、造池名 ─────────────────────────
class TestPoolName:
    @pytest.mark.parametrize("raw, expected", [
        ("Gemini Models", "Gemini"),
        ("Claude and GPT models", "Claude & GPT"),
        ("", ""),
        ("Gemini", "Gemini"),
        ("  Gemini Models  ", "Gemini"),
    ])
    def test_normalizes_group_name(self, raw, expected):
        assert quota._pool_name(raw) == expected


class TestVersionedAndPoolNames:
    def test_digit_marks_versioned(self):
        assert quota._versioned("Gemini 3") is True
        assert quota._versioned("Gemini") is False
        assert quota._versioned("") is False

    def test_versioned_pool_matches_prefix(self):
        assert quota._pool_names("Gemini 3", "Gemini 3 Pro (High)", "gemini-3-pro-high") is True
        assert quota._pool_names("Gemini 3", "Gemini 2.5 Pro", "gemini-2.5-pro") is False

    def test_non_versioned_pool_never_matches_by_name(self):
        """非版本化池名即使前缀命中也要返回 False——否则所有 Gemini 模型会被吸进同一个池。"""
        assert quota._pool_names("Gemini", "Gemini 3 Pro", "gemini-3-pro") is False


class TestSameReading:
    def test_percent_within_tolerance(self):
        # 差 0.4pp 是服务端取整噪声，必须容忍
        assert quota._same_reading(20.4, None, 20.0, None) is True

    def test_percent_outside_tolerance(self):
        # 差 0.6pp 已经说明是两个不同的池
        assert quota._same_reading(20.6, None, 20.0, None) is False

    def test_reset_within_a_minute(self):
        assert quota._same_reading(20.0, "2026-11-21T18:00:30Z", 20.0, _FIVE_RESET) is True

    def test_reset_beyond_a_minute(self):
        # 相差 2 分钟说明重置时刻不同，即使是同一个组也不能合并
        assert quota._same_reading(20.0, "2026-11-21T18:02:00Z", 20.0, _FIVE_RESET) is False

    def test_missing_reset_only_compares_percent(self):
        assert quota._same_reading(20.0, None, 20.0, _FIVE_RESET) is True
        assert quota._same_reading(20.0, _FIVE_RESET, 20.0, None) is True


class TestVendorOf:
    def test_known_prefixes(self):
        assert quota._vendor_of("gemini-3-flash", "Gemini 3 Flash") == "Gemini"
        assert quota._vendor_of("claude-opus-5-5-high", "Claude Opus 5.5 High") == "Claude"
        assert quota._vendor_of("gpt-oss-120b-medium", "GPT-OSS 120B") == "GPT-OSS"

    def test_falls_back_to_display_name_head(self):
        # 前缀认不出时用显示名首词，保证新家族也能对上组描述文本
        assert quota._vendor_of("qwen-3-max", "Qwen 3 Max") == "Qwen"

    def test_falls_back_to_id_first_segment(self):
        assert quota._vendor_of("mystery-1", "") == "mystery"


class TestModelPoolName:
    def test_strips_trailing_parenthesis(self):
        assert quota._model_pool_name("Claude Sonnet 4.6 (Thinking)") == "Claude Sonnet 4.6"
        assert quota._model_pool_name("Gemini 4 Argon (High)") == "Gemini 4 Argon"

    def test_keeps_name_without_parenthesis(self):
        assert quota._model_pool_name("Gemini 4 Argon") == "Gemini 4 Argon"
        assert quota._model_pool_name("") == ""


# ── catalog：family / 档位 ───────────────────────────────────────────────
class TestSplitFamily:
    def test_extra_low_beats_low(self):
        """最长优先：否则会被拆成 ("gemini-3.5-flash-extra", "low")，家族 id 就错了。"""
        assert catalog.split_family("gemini-3.5-flash-extra-low") == (
            "gemini-3.5-flash", "minimal")

    def test_extra_high_beats_high(self):
        assert catalog.split_family("gemini-3.5-flash-extra-high") == (
            "gemini-3.5-flash", "xhigh")

    def test_medium(self):
        assert catalog.split_family("gemini-3.8-flash-medium") == ("gemini-3.8-flash", "medium")

    def test_bare_id_has_no_level(self):
        assert catalog.split_family("gemini-3-flash") == ("gemini-3-flash", "")

    def test_xhigh(self):
        assert catalog.split_family("gemini-4-argon-xhigh") == ("gemini-4-argon", "xhigh")

    def test_family_of_returns_head(self):
        assert catalog.family_of("gemini-3.8-flash-high") == "gemini-3.8-flash"
        assert catalog.family_of("gemini-3-flash") == "gemini-3-flash"


class TestEffortRank:
    def test_order_is_ascending(self):
        ranks = [catalog.effort_rank(x) for x in ("minimal", "low", "medium", "high", "xhigh")]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == 5

    def test_unknown_level_ranks_last(self):
        # 未知档位排最后，保证 variants_by_effort 的「最强可用」不会选中它
        assert catalog.effort_rank("") > catalog.effort_rank("xhigh")


class TestVariantsByEffort:
    def test_bare_id_is_default(self):
        out = catalog.variants_by_effort(["gemini-3-flash"])
        assert out == {"": "gemini-3-flash"}

    def test_default_falls_back_to_high(self):
        # 无裸 id 时未指定档位等同于 high（网关的真实行为），不能取最强档
        out = catalog.variants_by_effort([
            "gemini-3.8-flash-low", "gemini-3.8-flash-high",
        ])
        assert out[""] == "gemini-3.8-flash-high"
        assert out["low"] == "gemini-3.8-flash-low"

    def test_default_falls_back_to_strongest_available(self):
        # 连 high 都没有时取最强可用档位（medium 强于 low）
        out = catalog.variants_by_effort([
            "gemini-3.8-flash-low", "gemini-3.8-flash-medium",
        ])
        assert out[""] == "gemini-3.8-flash-medium"
