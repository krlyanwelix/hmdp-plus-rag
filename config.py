"""集中配置。

所有可调参数汇集在这里，便于对照 README 的「调优速查」逐项试验。
读取项目根目录的 .env（若存在），不依赖 python-dotenv。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载：KEY=VALUE，忽略注释，不覆盖已存在的环境变量。"""
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _s(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # ---------------- 路径 ----------------
    raw_dir: str = _s("RAW_DIR", "data/raw")
    store_dir: str = _s("STORE_DIR", "data/index")

    # ---------------- 切块 ----------------
    # 父块：喂给 LLM 的完整上下文单元；子块：真正参与检索的最小语义单元。
    parent_size: int = _i("PARENT_SIZE", 1500)
    chunk_size: int = _i("CHUNK_SIZE", 500)
    chunk_overlap: int = _i("CHUNK_OVERLAP", 80)
    min_chunk_size: int = _i("MIN_CHUNK_SIZE", 40)

    # ---------------- 向量化 ----------------
    # auto: 能 import sentence_transformers 就用它，否则退回内置 hashing embedder。
    embedder: str = _s("EMBEDDER", "auto")          # auto | st | hashing | openai
    embed_model: str = _s("EMBED_MODEL", "BAAI/bge-m3")
    embed_dim: int = _i("EMBED_DIM", 1024)          # 仅 hashing embedder 使用
    # BGE 系列是非对称检索模型，查询端需要这个前缀，缺了会明显掉召回。
    embed_query_prefix: str = _s(
        "EMBED_QUERY_PREFIX", "为这个句子生成表示以用于检索相关文章："
    )

    # ---------------- 检索 ----------------
    top_k_recall: int = _i("TOP_K_RECALL", 30)      # 召回要宽，精度交给 rerank
    top_k_final: int = _i("TOP_K_FINAL", 5)
    use_bm25: bool = _b("USE_BM25", True)           # 混合检索开关
    rrf_k: int = _i("RRF_K", 60)                    # RRF 平滑常数

    # ---------------- 重排 ----------------
    # auto: 有 sentence_transformers 用 cross-encoder，否则用内置词法重排。
    reranker: str = _s("RERANKER", "auto")          # auto | cross | lexical | none
    rerank_model: str = _s("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

    # ---------------- 向量库 ----------------
    store_backend: str = _s("STORE_BACKEND", "auto")  # auto | memory | chroma

    # ---------------- 生成 ----------------
    llm_base_url: str = _s("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model: str = _s("LLM_MODEL", "deepseek-chat")
    llm_api_key: str = (
        _s("LLM_API_KEY", "")
        or _s("DEEPSEEK_API_KEY", "")
        or _s("OPENAI_API_KEY", "")
    )
    llm_temperature: float = _f("LLM_TEMPERATURE", 0.1)
    llm_timeout: int = _i("LLM_TIMEOUT", 60)

    # ---------------- 评测 ----------------
    eval_k: int = _i("EVAL_K", 5)
    eval_dataset: str = _s("EVAL_DATASET", "eval/dataset.jsonl")

    # ---------------- 派生路径 ----------------
    @property
    def raw_path(self) -> Path:
        p = Path(self.raw_dir)
        return p if p.is_absolute() else (ROOT / p)

    @property
    def store_path(self) -> Path:
        p = Path(self.store_dir)
        return p if p.is_absolute() else (ROOT / p)

    @property
    def eval_dataset_path(self) -> Path:
        p = Path(self.eval_dataset)
        return p if p.is_absolute() else (ROOT / p)
