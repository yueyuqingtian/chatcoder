"""chatcoder 服务端启动入口（v2）。

启动顺序:init_db(建表) → seed(幂等种子) → uvicorn(app)。
"""
import asyncio
import logging

import uvicorn

from app.core.config import settings
from app.core.logging import setup_logging
from app.persistence.database import init_db
from app.persistence.seed import seed


def main() -> None:
    setup_logging(debug=settings.debug)
    logging.getLogger("app").info("初始化数据库...")
    asyncio.run(init_db())
    stats = asyncio.run(seed())
    print(f"[seed] {stats}")
    uvicorn.run(
        "app.main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=settings.debug,
        # 打包环境下 httptools 的 C 扩展收集不可靠，统一用纯 Python 的 h11
        http="h11",
    )


if __name__ == "__main__":
    main()
