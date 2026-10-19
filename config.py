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
    raw_dir: str = _s("RAW_DIR", "data/sports")
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
    # API embedding（embedder=openai 时生效）。留空则复用 LLM 的 base_url/key。
    # 注意：DeepSeek 官方 API 不提供 /embeddings，需要单独的 embedding 服务。
    embed_base_url: str = _s("EMBED_BASE_URL", "")
    embed_api_key: str = _s("EMBED_API_KEY", "")

    # ---------------- 检索 ----------------
    top_k_recall: int = _i("TOP_K_RECALL", 30)      # 召回要宽，精度交给 rerank
    top_k_final: int = _i("TOP_K_FINAL", 5)
    use_bm25: bool = _b("USE_BM25", True)           # 混合检索开关
    rrf_k: int = _i("RRF_K", 60)                    # RRF 平滑常数

    # ---------------- 重排 ----------------
    # auto: 有 sentence_transformers 用 cross-encoder，否则用内置词法重排。
    # api: 走 /rerank 接口（硅基流动等），不占显存、不加载本地模型。
    reranker: str = _s("RERANKER", "auto")          # auto | cross | api | lexical | none
    rerank_model: str = _s("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    # API 重排（reranker=api 时生效）。留空则复用 embedding 的地址/key。
    rerank_base_url: str = _s("RERANK_BASE_URL", "")
    rerank_api_key: str = _s("RERANK_API_KEY", "")

    # ---------------- 设备 ----------------
    # auto: 有 CUDA 就用 GPU，否则 CPU；也可显式设 cuda / cpu。
    # 注意：要走 GPU 必须装 CUDA 版 torch（不是 +cpu 版），否则只有 CPU。
    device: str = _s("DEVICE", "auto")              # auto | cuda | cuda:0 | cpu

    # ---------------- 向量库 ----------------
    store_backend: str = _s("STORE_BACKEND", "auto")  # auto | memory | chroma | milvus
    # 注意：不要用 MILVUS_URI 这个名字 —— 它被 pymilvus 自己占用（全局默认连接），
    # 设成本地文件路径会直接让它 import 时抛 "Illegal uri"。
    milvus_uri: str = _s("RAG_MILVUS_URI", _s("MILVUS_DB_PATH", "data/milvus.db"))
    milvus_collection: str = _s("MILVUS_COLLECTION", "rag_chunks")
    # 距离度量：bge-m3 向量已归一化，COSINE 与 IP 等价；换模型时改成 L2 即可
    milvus_metric: str = _s("MILVUS_METRIC", "COSINE")

    # ---------------- 答案缓存 ----------------
    cache_ttl: int = _i("RAG_CACHE_TTL", 300)
    # 含个人报名信息的答案不留在缓存里太久（也加密存储，见 rag/cache.py）
    cache_user_ttl: int = _i("RAG_CACHE_USER_TTL", 60)
    cache_encrypt: bool = _b("RAG_CACHE_ENCRYPT", True)
    cache_key: str = _s("RAG_CACHE_KEY", "")

    # ---------------- 限流 ----------------
    # 应用层按 userId 令牌桶；网关层（OpenResty）另有全局桶，两层目标不同
    rate_limit_enabled: bool = _b("RAG_RATE_LIMIT_ENABLED", True)
    rate_limit_per_min: int = _i("RAG_RATE_LIMIT_PER_MIN", 20)
    rate_limit_burst: int = _i("RAG_RATE_LIMIT_BURST", 5)

    # ---------------- 生成超时 ----------------
    # 非流式整体超时
    llm_timeout: int = _i("LLM_TIMEOUT", 45)
    # 流式：每次 socket 读的最大等待，覆盖"上游卡住一个字节都不给"
    llm_stream_timeout: int = _i("LLM_STREAM_TIMEOUT", 20)
    # 流式：整个生成阶段的墙钟上限，覆盖"上游慢速滴灌把连接拖到天荒地老"
    llm_deadline: int = _i("LLM_DEADLINE", 45)

    # ---------------- 生成 ----------------
    llm_base_url: str = _s("LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model: str = _s("LLM_MODEL", "deepseek-chat")
    llm_api_key: str = (
        _s("LLM_API_KEY", "")
        or _s("DEEPSEEK_API_KEY", "")
        or _s("OPENAI_API_KEY", "")
    )
    llm_temperature: float = _f("LLM_TEMPERATURE", 0.1)
    # 成本估算用单价（每百万 token），单位随你（一般用元）。留空则不显示成本。
    llm_price_input: float = _f("LLM_PRICE_INPUT", 0.0)
    llm_price_output: float = _f("LLM_PRICE_OUTPUT", 0.0)

    # ---------------- 评测 ----------------
    eval_k: int = _i("EVAL_K", 5)
    eval_dataset: str = _s("EVAL_DATASET", "eval/dataset-sports.jsonl")

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
