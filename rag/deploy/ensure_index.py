"""容器启动时判断索引是否已经建好。

退出码 0 = 集合里已有数据，跳过重建；退出码 3 = 空集合（或连不上），让 entrypoint 重建。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from rag.index.store import get_store  # noqa: E402


def main() -> int:
    cfg = Config()
    try:
        store = get_store(cfg, verbose=False)
        n = store.count()
    except Exception:  # Milvus 刚起来还没 ready 时也会走到这里
        traceback.print_exc()
        return 3
    print(f"[ensure_index] backend={cfg.store_backend} chunks={n}", flush=True)
    return 0 if n > 0 else 3


if __name__ == "__main__":
    sys.exit(main())
