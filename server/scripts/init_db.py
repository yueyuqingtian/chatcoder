"""初始化数据库 schema（开发环境用，生产用 Alembic 迁移）。"""
import asyncio

from app.persistence.database import Base, engine
from app.persistence.models import *  # noqa: F401,F403  确保所有模型被注册


async def init() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("[init_db] schema created.")


if __name__ == "__main__":
    asyncio.run(init())
