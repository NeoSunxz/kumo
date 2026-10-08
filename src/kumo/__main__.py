import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .web import create_app


def app() -> FastAPI:
    """uvicorn 的应用工厂; 热加载时每次重启都在新进程里调用, 日志配置也放在这里."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


def main() -> None:
    host = os.environ.get("KUMO_HOST", "0.0.0.0")
    port = int(os.environ.get("KUMO_PORT", "8115"))
    if os.environ.get("KUMO_RELOAD") == "1":
        # 开发用: 源码挂载进容器, .py 改动后自动重启 (重启会中断正在运行的任务); 模板改动不需要重启.
        uvicorn.run("kumo.__main__:app", factory=True, host=host, port=port, reload=True, reload_dirs=[str(Path(__file__).parent)])
    else:
        uvicorn.run(app(), host=host, port=port)


if __name__ == "__main__":
    main()
