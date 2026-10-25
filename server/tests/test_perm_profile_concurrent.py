"""权限模式配置并发保存的回归用例。

缺陷原貌：`upsert_profile` / `delete_profile` 是 `_read_cfg()` → 改 list → `_write_cfg()`
（整文件覆盖，无锁、无临时文件）。两个请求并发时，后写的用自己读到的**旧快照**覆盖掉
对方刚写入的 profile——用户看到「刚保存了 A，B 却不见了」。另外 `_write_cfg` 直接
`write_text`，先截断后写，中途崩溃会留下半截 JSON，下次读解析失败返回 `{}`，
所有自定义模式一次性消失且无任何报错。

本文件钉住四件事：
1. 线程并发 upsert 两个不同 profile，最终文件里**两个都在**；
2. asyncio 并发（同一事件循环内多个协程）同样不丢；
3. 原子写路径下不留 `*.tmp` 残留；
4. 判定档缓存按配置文件指纹失效——别的进程改了 kind，本进程下一次判定就能读到。
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from app.services import permission_profile_service as pps


@pytest.fixture(autouse=True)
def profiles(tmp_path, monkeypatch) -> Path:
    """把用户配置指向临时文件；每个用例从干净状态（含缓存）开始。

    `_CFG_PATH` 是模块级常量，`_read_cfg()` 每次调用都重新读它，因此改常量立即生效，
    不会污染真实配置（与 `test_custom_mode_boundary.py` 同一手法）。
    """
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pps, "_CFG_PATH", cfg)
    pps._clear_mode_kind_cache()
    yield cfg
    pps._clear_mode_kind_cache()


def _names(cfg: Path) -> set[str]:
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return {str(p["name"]) for p in data.get("permission_profiles", []) if isinstance(p, dict)}


def test_concurrent_thread_upserts_keep_both_profiles(profiles):
    """线程并发 upsert 两个不同 profile：最终文件里两个都在，没有丢失。"""
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=5)  # 尽量让两个写路径真正撞在一起
            pps.upsert_profile({"name": name, "kind": "readonly", "tools": []})
        except BaseException as exc:  # noqa: BLE001 —— 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发保存抛错：{errors}"
    assert _names(profiles) == {"alpha", "beta"}, (
        f"并发保存丢失配置，文件里只有 {_names(profiles)}"
    )


def test_concurrent_thread_upserts_many_profiles(profiles):
    """压力更足的一组：8 个线程同时写不同 profile，一个都不能丢。"""
    errors: list[BaseException] = []
    names = [f"mode_{i}" for i in range(8)]
    barrier = threading.Barrier(len(names))

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=5)
            pps.upsert_profile({"name": name, "kind": "plan", "tools": ["fs_read"]})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"并发保存抛错：{errors}"
    assert _names(profiles) == set(names), f"并发保存丢了配置：{set(names) - _names(profiles)}"


@pytest.mark.asyncio
async def test_async_concurrent_upserts_keep_both_profiles(profiles):
    """asyncio 并发（同一事件循环内 gather）同样不丢配置。"""
    await asyncio.gather(
        asyncio.to_thread(pps.upsert_profile, {"name": "alpha", "kind": "readonly", "tools": []}),
        asyncio.to_thread(pps.upsert_profile, {"name": "beta", "kind": "plan", "tools": []}),
    )

    assert _names(profiles) == {"alpha", "beta"}


def test_upsert_and_delete_do_not_leave_tmp_files(profiles):
    """原子写路径不留 `*.tmp` 残留（写到一半崩溃会污染目录）。"""
    pps.upsert_profile({"name": "alpha", "kind": "readonly", "tools": []})
    pps.upsert_profile({"name": "beta", "kind": "full", "tools": []})
    assert pps.delete_profile("alpha") is True

    leftovers = [p.name for p in profiles.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"原子写残留临时文件：{leftovers}"
    # 配置本身必须是完整可解析的 JSON
    assert _names(profiles) == {"beta"}


def test_delete_missing_profile_keeps_file_intact(profiles):
    """删除不存在的 profile 返回 False 且不改文件（临界区内提前返回）。"""
    pps.upsert_profile({"name": "alpha", "kind": "readonly", "tools": []})
    before = profiles.read_text(encoding="utf-8")

    assert pps.delete_profile("no_such_mode") is False
    assert profiles.read_text(encoding="utf-8") == before


def test_mode_kind_cache_invalidated_by_external_write(profiles):
    """其它进程改了配置文件：本进程的判定档缓存必须按指纹失效。"""
    pps.upsert_profile({"name": "review_only", "kind": "readonly", "tools": []})
    assert pps.resolve_mode_kind("review_only") == pps.MODE_KIND_READONLY  # 建立缓存

    # 模拟另一个进程（索引 worker / 另一实例）直接改配置文件：kind 变 full
    data = json.loads(profiles.read_text(encoding="utf-8"))
    for p in data["permission_profiles"]:
        if p["name"] == "review_only":
            p["kind"] = "full"
    pps._write_cfg(data)

    assert pps.resolve_mode_kind("review_only") == pps.MODE_KIND_AGENT, (
        "别的进程改了 kind，本进程仍按旧缓存判定——审批边界会一直用旧口径"
    )


def test_local_upsert_invalidates_cache(profiles):
    """本进程 upsert 改 kind：缓存必须立即失效（原有行为不能退化）。"""
    pps.upsert_profile({"name": "draft_plan", "kind": "readonly", "tools": []})
    assert pps.resolve_mode_kind("draft_plan") == pps.MODE_KIND_READONLY

    pps.upsert_profile({"name": "draft_plan", "kind": "plan", "tools": []})
    assert pps.resolve_mode_kind("draft_plan") == pps.MODE_KIND_PLAN


def test_write_cfg_is_atomic_and_readable(profiles):
    """`_write_cfg` 写出的文件可被 `_read_cfg` 原样读回（含中文与嵌套结构）。"""
    payload = {"permission_profiles": [{"name": "中文模式", "hint": "保留中文，不要转义"}], "x": 1}
    pps._write_cfg(payload)

    assert pps._read_cfg() == payload
    raw = profiles.read_text(encoding="utf-8")
    assert "中文模式" in raw, "写出的 JSON 应保留中文（ensure_ascii=False）"
