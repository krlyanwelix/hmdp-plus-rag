"""容器启动时判断索引是否已经建好。

退出码 0 = 集合里有数据且父块文件在，跳过重建；
退出码 3 = 集合为空 / 父块文件缺失 / 连不上，让 entrypoint 重建。

为什么要连带检查 parents.json：父块由 ParentStore 单独存在
`data/index/parents.json`（不进向量库），而向量库在另一个数据卷里。
容器被删掉重建时如果只持久化了 Milvus，父块就丢了 —— 检索仍能跑
（retriever 会退回子块），但 small-to-big 的上下文就没了，而且
集合非空所以永远不会自动重建。两个一起检查才能自愈。
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

    parents_file = Path(cfg.store_dir) / "parents.json"
    has_parents = parents_file.exists() and parents_file.stat().st_size > 2
    print(
        f"[ensure_index] backend={cfg.store_backend} chunks={n} "
        f"parents={'yes' if has_parents else 'no'} ({parents_file})",
        flush=True,
    )
    if n <= 0:
        return 3
    if not has_parents:
        # 父块文件丢了但向量库还在：重建一次，把父块补回来
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
