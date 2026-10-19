"""RAG 服务启动入口。

不直接用 `python -m uvicorn` 的原因：依赖装在项目自带的 `.pylibs` 里，
`-m` 启动时解释器还没加过这个路径，会报 `No module named uvicorn`。
这里先把 `.pylibs` 和项目根目录塞进 sys.path，再拉起 uvicorn。

用法：
    E:\\Python312\\python.exe service\\run.py
可选环境变量：RAG_HOST（默认 127.0.0.1）、RAG_PORT（默认 8090）、RAG_LOG_LEVEL（默认 info）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".pylibs"))
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "service.api:app",
        host=os.getenv("RAG_HOST", "127.0.0.1"),
        port=int(os.getenv("RAG_PORT", "8090")),
        log_level=os.getenv("RAG_LOG_LEVEL", "info"),
        access_log=True,
    )
