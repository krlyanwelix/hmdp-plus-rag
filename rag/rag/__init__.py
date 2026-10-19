"""纯 Python RAG 骨架。

两条链路，唯一耦合点是向量库：
    离线索引：parse -> split -> embed -> store
    在线查询：rewrite? -> hybrid retrieve -> rerank -> assemble -> generate
"""

__version__ = "0.1.0"
