"""验证真实模型：bge-m3 embedding + bge-reranker-v2-m3 重排。

手动跑： python scripts/verify_models.py
首次运行会从 HF 镜像下载模型（约 4.4GB），缓存在 .hf_cache/。
官方 huggingface.co 不通的环境请确认 HF_ENDPOINT 指向可用镜像。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pylibs"))

# 必须在 import sentence_transformers 之前设置，huggingface_hub 在 import 时固定 endpoint
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".hf_cache"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from rag.index.embed import SentenceTransformerEmbedder  # noqa: E402
from rag.retrieve.rerank import CrossEncoderReranker  # noqa: E402
from rag.types import Hit  # noqa: E402

t0 = time.time()
print("[1] 加载 bge-m3 embedding ...", flush=True)
emb = SentenceTransformerEmbedder("BAAI/bge-m3", "为这个句子生成表示以用于检索相关文章：")
print(f"    dim={emb.dim}  耗时 {time.time() - t0:.1f}s", flush=True)

vecs = emb.encode(["XR-2000 的最大并发连接数是多少", "这是一条完全无关的测试文本"], is_query=True)
print(f"    encode shape={vecs.shape}", flush=True)
sim = float(vecs[0] @ vecs[1])
print(f"    相似度(无关句)={sim:.4f}  归一化范数={[round(float(__import__('numpy').linalg.norm(v)), 3) for v in vecs]}", flush=True)

t0 = time.time()
print("[2] 加载 bge-reranker-v2-m3 ...", flush=True)
rr = CrossEncoderReranker("BAAI/bge-reranker-v2-m3")
print(f"    耗时 {time.time() - t0:.1f}s", flush=True)

hits = [
    Hit("correct", 0.0, "XR-2000 最大并发连接数 5000，整机吞吐 20 Gbps", {}),
    Hit("wrong", 0.0, "XR-1000 最大并发连接数 1200，整机吞吐 5 Gbps", {}),
]
top = rr.rerank("XR-2000 的最大并发连接数是多少", hits, 2)
print("    rerank 顺序:", [(h.chunk_id, round(h.score, 4)) for h in top], flush=True)
print("    期望 correct 排第一:", top[0].chunk_id == "correct", flush=True)

print("\n[OK] 两个真实模型都可用", flush=True)
