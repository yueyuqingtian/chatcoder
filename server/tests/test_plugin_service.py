"""插件服务测试（plan-282-1441 #6）。

覆盖：内置目录可读、manifest 校验、目录安装→贡献技能注册→启停→卸载的闭环。
不接线上市场，因此全部基于临时目录，不依赖网络。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Skill  # noqa: F401 注册模型
from app.services import plugin_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/plugin.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    """把插件根目录重定向到临时目录，避免污染真实 ~/.chatcoder。"""
    root = tmp_path / "plugins-home"
    root.mkdir()
    monkeypatch.setattr(plugin_service, "_PLUGINS_ROOT", root)
    monkeypatch.setattr(plugin_service, "_REGISTRY_FILE", root / "installed_plugins.json")
    return root


def _make_plugin(dir_path: Path, name: str = "demo") -> Path:
    """造一个含 plugin.json 与 skills/*.md 的插件目录。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "plugin.json").write_text(json.dumps({
        "name": name,
        "displayName": "Demo Plugin",
        "version": "1.2.3",
        "description": "demo",
        "descriptionZh": "演示插件",
        "author": {"name": "tester"},
        "category": "代码开发",
        "tags": ["demo"],
        "skills": "./skills/",
        "marketplaceName": "local",
        "defaultEnabled": True,
    }, ensure_ascii=False), encoding="utf-8")
    skills = dir_path / "skills"
    skills.mkdir(exist_ok=True)
    (skills / "helper.md").write_text(
        "---\nname: 助手技能\ndescription: 帮助做某事\n---\n正文指令\n", encoding="utf-8")
    return dir_path


def test_catalog_is_readable():
    """内置目录必须是合法 JSON 且至少有一条（市场页的初始内容）。"""
    items = plugin_service.read_catalog()
    assert len(items) > 0
    assert all(i.get("name") for i in items)


def test_manifest_validation(plugin_home, tmp_path):
    """缺少 plugin.json 或 name 缺失的目录不得被当成插件。"""
    bad = tmp_path / "bad"
    bad.mkdir()
    assert plugin_service._read_manifest(bad) is None

    (bad / "plugin.json").write_text('{"version":"1"}', encoding="utf-8")
    assert plugin_service._read_manifest(bad) is None

    good = _make_plugin(tmp_path / "good", "ok")
    assert plugin_service._read_manifest(good)["name"] == "ok"


@pytest.mark.asyncio
async def test_install_enable_uninstall_cycle(db, plugin_home, tmp_path):
    """安装 → 贡献技能入库 → 启停同步 → 卸载并注销技能。"""
    src = _make_plugin(tmp_path / "src", "cycle")

    res = await plugin_service.install_from_dir(db, str(src))
    assert res["ok"] is True
    assert res["skills"] >= 1, "插件的 skills/*.md 应被注册为技能"

    # 技能以 <plugin>: 前缀入库
    rows = (await db.execute(select(Skill).where(Skill.source == "plugin"))).scalars().all()
    names = [r.name for r in rows]
    assert any(n.startswith("cycle:") for n in names), f"实际技能名 {names}"

    # 已安装列表可见
    installed = plugin_service.list_installed()
    assert any(p["name"] == "cycle" for p in installed)

    # 停用 → 技能同步失效
    await plugin_service.set_enabled(db, "cycle", False)
    rows = (await db.execute(select(Skill).where(Skill.source == "plugin"))).scalars().all()
    assert all(r.is_active is False for r in rows if str(r.name).startswith("cycle:"))

    # 卸载 → 技能被注销、注册表条目移除
    out = await plugin_service.uninstall(db, "cycle")
    assert out["ok"] is True
    rows = (await db.execute(select(Skill).where(Skill.source == "plugin"))).scalars().all()
    assert not [r for r in rows if str(r.name).startswith("cycle:")]
    assert not any(p["name"] == "cycle" for p in plugin_service.list_installed())


@pytest.mark.asyncio
async def test_install_rejects_non_plugin_dir(db, plugin_home, tmp_path):
    """非插件目录安装必须报错（明确提示缺 plugin.json）。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        await plugin_service.install_from_dir(db, str(plain))


def test_marketplace_marks_installed(plugin_home, tmp_path):
    """市场视图应把已安装的条目标注为 installed（供 UI 显示"已安装/已启用"）。"""
    vm = plugin_service.list_marketplace()
    assert vm["ok"] is True
    assert vm["installedCount"] == 0
    assert all(i.get("installed") is False for i in vm["items"])
