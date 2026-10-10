# 纯 Python RAG 骨架

一套**零依赖即可跑通**的 RAG 骨架：解析 → 切块 → 向量化 → 混合检索 → 重排 → 生成 → 引用溯源，
外加一个能给出可比较数字的检索评测。

设计取向是**每个环节都可插拔、可观测**，而不是堆一个开箱即用的黑盒。
默认用内置的降级实现跑起来，装上真实组件后自动切换，代码不用改。

> 当前工作区已装好全部真实组件（chromadb + bge-m3 + bge-reranker-v2-m3 + jieba 等），
> `auto` 会自动使用它们。实测数据与结论见「已验证」章节。

---

## 快速开始

```bash
# 只依赖 numpy，本环境已内置
python cli.py ingest                 # 建索引
python cli.py search "E1042 是什么"   # 只看检索结果（调检索时最常用）
python cli.py ask "E1042 是什么"      # 完整问答
python cli.py eval --compare         # 三模式消融对比
python cli.py chunks                 # 人工检查切块是否合理
```

`data/raw/` 下已放好 7 篇示例文档（虚构的 XR-2000 网关产品资料），
其中特意包含一篇**上一代 XR-1000 手册作为干扰项**——它们的配置项名称相同但默认值不同，
用来检验检索能不能区分「相似但错误」的内容。

想接真实 LLM 生成答案：

```bash
cp .env.example .env      # 然后填 LLM_API_KEY
```

不配也能跑，此时 `ask` 会退化成「只展示检索到的原文」——这正是调检索时想要的行为。

---

## 架构

两条链路，唯一耦合点是向量库：

```
【离线索引】 python cli.py ingest
  文档 → 解析 → 切块(父/子) → 向量化 → 向量库
                        └────────────→ 父块存储

【在线查询】 python cli.py ask
  问题 ─→ 向量召回 ─┐
                    ├→ RRF 融合 → 重排 → 取回父块 → prompt → LLM → 引用
  问题 ─→ BM25 召回 ─┘
```

### 目录

```
config.py                    所有可调参数集中在这里
cli.py                       命令行入口
rag/
  types.py                   Document / Chunk / Hit / Context / Answer
  ingest/
    parse.py                 多格式解析 + 结构锚点（标题、页码）
    split.py                 递归切分 + 父子块，返回字符偏移
  index/
    embed.py                 Embedder 工厂（hashing / sentence-transformers / openai）
    store.py                 向量库工厂（memory 暴力检索 / chroma HNSW）
    parents.py               父块存储（只按 id 取回，不进向量库）
  retrieve/
    bm25.py                  BM25 倒排索引（可选 jieba 分词）
    fusion.py                RRF 融合
    rerank.py                Reranker（none / lexical / cross-encoder）
    retriever.py             检索编排
  generate/
    prompt.py                上下文组装 + lost-in-the-middle 重排 + 引用解析
    llm.py                   OpenAI 兼容客户端（urllib，无第三方依赖）
  indexer.py                 离线链路编排
  pipeline.py                在线链路编排（门面）
  eval/metrics.py            检索层评测指标
data/raw/                    示例语料
eval/dataset.jsonl           评测集（20 题，含 1 个负样本）
```

### 几个刻意的设计决定

**父块不进向量库。** 父块只按 id 取回、从不参与相似度计算，
放进向量库等于浪费向量维度和检索时间。单独用 JSON 存，逻辑也更清楚。

**切分函数返回字符偏移。** 这样才能给每个**子块**标注它真正所属的章节，
而不是让它继承父块的章节。早期版本偷懒继承父块标题，结果 1500 字的父块里
所有子块都显示同一个标题，引用溯源直接失效。

**BM25 每次启动重建。** 语料不大时重建很快，省掉一套索引持久化和失效逻辑。
语料上规模后再改成落盘。

---

## 命令

| 命令 | 作用 |
| --- | --- |
| `ingest [--rebuild] [--raw DIR]` | 建索引。默认增量（按 source 先删后加），`--rebuild` 全量重建 |
| `stats` | 查看索引数量与各组件实际生效的实现 |
| `search "问题" [--mode M] [-k N] [--full]` | 只检索不生成 |
| `ask "问题" [--mode M] [--show-context]` | 完整问答 |
| `eval [--compare] [-k N] [--fail-under X]` | 跑评测；`--compare` 对比 vector/bm25/hybrid |
| `chunks [--limit N]` | 打印切块结果，人工检查粒度 |

`--mode` 可选 `hybrid`（默认）/ `vector` / `bm25`。全局 `-q` 可放在子命令前后任一位置。

**换 embedding 模型后必须 `--rebuild`**，因为新旧向量不在同一个语义空间，混在一起检索会失真。

---

## 配置

全部通过环境变量或 `.env` 覆盖，见 `.env.example`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EMBEDDER` | auto | auto / st / hashing / openai |
| `EMBED_MODEL` | BAAI/bge-m3 | 本地 embedding 模型 |
| `EMBED_QUERY_PREFIX` | BGE 中文前缀 | **非对称检索前缀，删掉会明显掉召回** |
| `STORE_BACKEND` | auto | auto / memory / chroma |
| `RERANKER` | auto | auto / cross / lexical / none |
| `PARENT_SIZE` | 1500 | 父块目标字数（喂给 LLM 的单元） |
| `CHUNK_SIZE` | 500 | 子块目标字数（检索单元） |
| `CHUNK_OVERLAP` | 80 | 子块重叠字数 |
| `TOP_K_RECALL` | 30 | 每路召回的宽度 |
| `TOP_K_FINAL` | 5 | 重排后保留数 |
| `USE_BM25` | true | 混合检索开关 |
| `RRF_K` | 60 | RRF 平滑常数 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | DeepSeek | 任何 OpenAI 兼容服务 |

---

## 调优工作流

**不要凭感觉调。** 这个骨架给了你两条腿走路：`search` 看单题，`eval` 看整体。

```bash
# 1. 先建基线
python cli.py eval --mode hybrid -k 5

# 2. 改一个参数，重跑，看「证据MRR」变了没有
python cli.py eval --mode hybrid -k 5

# 3. 怀疑某一路召回没用，做消融
python cli.py eval --compare
```

按症状定位：

| 症状 | 最可能的原因 | 对策 |
| --- | --- | --- |
| 完全找不到相关文档 | 切块太碎 / 没开 BM25 | 加大 `CHUNK_SIZE`、确认 `USE_BM25=true` |
| 找到了但排在后面 | 缺重排 / 缺查询前缀 | 上 cross-encoder、确认 `EMBED_QUERY_PREFIX` |
| 型号、错误码搜不到 | 纯向量的固有盲区 | 必须开混合检索（BM25） |
| 答案答非所问 | 上下文污染 | 调小 `TOP_K_FINAL` |
| 答案像编的 | 无溯源约束 | 见 `rag/generate/prompt.py` 的 system prompt |
| 文档更新后答旧内容 | 索引未更新 | 重跑 `ingest`（增量） |

**一次只改一个变量。** 同时改 `CHUNK_SIZE` 和 `TOP_K_RECALL`，你无法知道是哪个起了作用。

---

## 评测怎么读

`eval` 同时给出五个指标，因为它们回答的是不同问题：

| 指标 | 回答的问题 |
| --- | --- |
| `recall@k` | 正确的**来源文件**进前 k 了吗 |
| `证据召回` | 答案**所在的那段话**进来了吗（不看排名） |
| `证据@1` | 答案就在**第一段**吗 |
| `证据MRR` | 答案平均排在第几段 |
| `来源MRR` / `ndcg` | 正确来源排得够不够靠前 |

**为什么不能只看 `recall@k`：** 语料小的时候它会饱和。
本骨架的示例语料里，`TOP_K_FINAL=5` 已经覆盖了相当比例的父块，来源级 recall 很容易接近 1.0，
数字好看但没有区分度。所以要盯 **`证据MRR`** 和 **`证据@1`**——它们对排序敏感。

标注格式（`eval/dataset.jsonl`，每行一个 JSON）：

```json
{"question": "...", "relevant_sources": ["a.md"], "evidence": ["5000"], "gold_answer": "5000"}
```

- `evidence`：必须**同时**出现在同一个检索上下文里的要点字符串。
  标注时挑真正能定位答案的短串（型号、参数值、专有名词），别用「的」「是」这种到处都有的词。
- `relevant_sources` 留空、`evidence` 也留空 = **负样本**，用于测生成端幻觉，不计入检索指标。

`--fail-under X` 会在证据MRR 低于 X 时返回退出码 1，可以挂进 CI。

---

## 已验证 / 未验证

**所有组件均已实跑验证**（chromadb 1.5.9 + sentence-transformers 6.1.0 + torch 2.14 + pypdf + beautifulsoup4 + jieba）：

| 组件 | 实现 | 状态 |
| --- | --- | --- |
| 解析 .txt/.md/.docx/.pdf/.html | `parse.py` | ✅ 全部实测（含标题/分页锚点提取） |
| 切块（父子块 + 字符偏移） | `split.py` | ✅ |
| embedding | `HashingEmbedder` + `SentenceTransformerEmbedder`(bge-m3) | ✅ 都实测 |
| 向量库 | `MemoryStore` + `ChromaStore`(HNSW) | ✅ 都实测，结果一致 |
| 关键词检索 | `BM25Index`（jieba 分词生效） | ✅ |
| 融合 / 重排 | RRF + Noop / Lexical / CrossEncoder(bge-reranker-v2-m3) | ✅ 都实测 |
| 评测 | recall@k / MRR / NDCG / 证据@1 / 证据MRR | ✅ |

**唯一未验证**：`OpenAIEmbedder` 和 `OpenAICompatLLM`——它们需要的不是 pip 包，而是
**API key + 一个真实或本地 LLM 服务**，本环境无法凭空验证。配置 `.env` 的 `LLM_API_KEY` 后即可用。

### 实测数据（20 题，k=5，chroma 后端）

**① 完整栈（bge-m3 embedding + bge-reranker 重排）**

```
mode          recall@5   证据召回   证据@1   证据MRR   来源MRR   ndcg
vector/bm25/hybrid   0.950   1.000   1.000   1.000   0.925   0.933   （三模式相同）
```

**② 纯召回层（`RERANKER=none`，观察召回本身的差异）**

```
mode        recall@5   证据召回   证据@1   证据MRR   来源MRR   ndcg
vector         0.908     1.000   0.947   0.974   0.867   0.865
bm25           0.950     1.000   1.000   1.000   0.900   0.911
hybrid         0.933     1.000   0.947   0.974   0.875   0.886
```

**③ 降级栈（hashing embedder + 词法重排，作对照）**：证据@1 = 0.895，证据MRR = 0.947。

### 三个结论（都来自实测，后两条反直觉）

1. **cross-encoder 重排带来真实收益**：证据@1 从 0.895 → 1.000、证据MRR 0.947 → 1.000。
   那两道原本排到第 2 段的题，加真实重排后全部升到第 1 段。

2. **cross-encoder 会把召回差异抹平**：②里三模式差异明显，①里三模式完全相同。
   重排太强，无论候选来自哪一路召回，都能挑出真正相关的段落。因此
   「装上语义模型后三模式差异就会显现」是错的——差异只在**关掉重排**时才可见。

3. **BM25 在这个语料上比语义向量强**：②里 bm25 全面领先 vector，hybrid 居中。
   原因是评测集几乎全是精确术语（型号、错误码、参数名、数字），BM25 词面匹配天然占优，
   语义向量反而把「语义相似但术语不对」的段落（XR-1000 vs XR-2000 的相似表述）拉了进来。
   **混合检索不是万能药**，价值取决于查询分布：精确术语多就偏 BM25，口语化/同义改写多就偏语义向量。

### 工程数据

- cross-encoder 重排 20 题（每题 30 候选 × 3 模式）：**CPU 约 10 分钟，GPU（RTX 4060）约 103 秒**。
- 纯重排推理单次（30 候选，模型已加载）：**GPU 121 ms，CPU 1411 ms，约 11.6 倍**。
  模型加载本身（读 2.2GB×2 权重到内存/显存）约 40 秒，与设备无关，是单次 search 的大头。
- 磁盘占用：模型缓存 `.hf_cache` 约 **6.5 GB**，依赖 `.pylibs` 约 **1.3 GB**（CUDA 版 torch 另加约 2.6 GB）。
- memory（精确）与 chroma（HNSW 近似）在 28 条向量上指标逐位一致，ANN 召回损失为零。

### GPU 重排怎么开

默认 `DEVICE=auto`，装上 CUDA 版 torch 就会自动用 GPU（`stats` 显示 `device: cuda:0`）。三步：

```bash
# 1) 把 CPU 版 torch 换成 CUDA 版（按需选 cu126 / cu124，对应 CUDA 12.x 驱动）
pip install --target .pylibs --force-reinstall torch --index-url https://download.pytorch.org/whl/cu126

# 2) 确认 GPU 可用
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"

# 3) 无需改代码，auto 自动走 GPU；也可显式指定
#    .env: DEVICE=cuda   （或 DEVICE=cpu 强制回退 CPU）
```

> 注意：CUDA 版 torch 的体积比 CPU 版大得多（本机实测约 2.6 GB），
> 且必须与 NVIDIA 驱动兼容。本机 RTX 4060 + 驱动 591.59 + torch 2.14.1+cu126 实测通过。
> 没 NVIDIA 显卡的机器装 CUDA 版 torch 是浪费，保持 CPU 版即可。


---

## 可选依赖装在哪

不想污染共享 Python 运行时的话，装到工作区本地目录：

```bash
pip install --target .pylibs chromadb sentence-transformers pypdf beautifulsoup4 jieba
```

> 上面装的是 CPU 版 torch（sentence-transformers 的默认依赖）。
> 要 GPU 加速重排，见上文「GPU 重排怎么开」单独换 CUDA 版 torch。

`cli.py` 启动时会自动把 `.pylibs` 追加到 `sys.path`，所以直接
`python cli.py stats` 就能看到它生效，不需要设 `PYTHONPATH`。
该目录已在 `.gitignore` 里。

**模型下载走镜像**：`sentence-transformers` 首次运行会从 HuggingFace 下载模型
（bge-m3 + bge-reranker-v2-m3 约 4.4GB）。官方 `huggingface.co` 不通时，在 `.env` 里设置：

```
HF_ENDPOINT=https://hf-mirror.com
HF_HOME=E:\dsh workspace\.hf_cache     # 缓存在工作区内，便于复用
```

装完 chromadb 后注意：`STORE_BACKEND=auto` 会**自动从 memory 切到 chroma**，
而新后端的集合是空的。程序会打印明确警告并提示重建，但重建仍然必须由你触发。

### 怎么观察「召回层」本身的差异

重排会把召回差异抹平，所以要看某一路召回到底有没有用，先关掉重排：

```bash
# Linux / macOS
RERANKER=none python cli.py eval --compare -k 5
# Windows PowerShell
$env:RERANKER="none"; python cli.py eval --compare -k 5
```

这样三模式的分数才是**召回层**的真实差异（见上文实测数据 ②）。
不加 `RERANKER=none` 时，看到的是「召回 + 重排」的最终效果（实测数据 ①）。

---

## 切换后端 / 模型

依赖装好后 `auto` 会自动切换，无需改代码。但切换后**必须重建索引**：

```bash
python cli.py ingest --rebuild
```

**不重建，程序会报警。** 索引目录里存了 `index_meta.json`，记录这份索引用什么后端、
什么 embedding 模型建的；检测到与当前配置不一致时，`search` / `eval` / `stats` 都会打印
明确的迁移提示。这个检查**不受 `-q` 影响**——它是正确性问题，不是日志噪音。

`stats` 命令会显示每个环节实际生效的实现：

```
  embedder          : sentence-transformers
  reranker          : cross-encoder
  store             : chroma
  llm               : echo
```

如果某项显示的还是降级实现，说明对应依赖没装上或导入失败，留意启动时的告警行。

---

## 扩展点

按性价比排序：

1. **查询改写**（`rag/retrieve/`）。多查询扩展或 HyDE：先让 LLM 编一个假答案，
   用它去检索。解决「问题」和「陈述」形态不一致的非对称检索问题。
   在 `Retriever.search` 里对 `question` 做扩展，把多路结果一起塞进 `rankings` 即可。
2. **评测集扩充。** 20 题只能做冒烟测试。真实项目至少 50–100 题，
   而且要从**线上真实提问**里采样，不要自己编——自己编的题会不自觉地避开难点。
3. **生成端指标**（RAGAS 的 faithfulness / answer relevancy）。
   需要 LLM 当裁判，在 `rag/eval/metrics.py` 里加一个 `judge` 层即可，
   骨架已经留了 `negative` 样本的挂点。
4. **BM25 落盘。** 语料上规模后，把 `BM25Index` 序列化，避免每次启动重建。
5. **流式输出。** `OpenAICompatLLM.chat` 改成 `stream=True` 逐块返回。
6. **增量更新的内容比对。** 现在按 source 先删后加，文档没变也会重新向量化。
   加上内容哈希可以跳过未变更的文档。

---

## 已知限制

- **没有并发与缓存。** 单进程同步实现，适合验证与轻量使用，不是服务端骨架。
- **没有权限与多租户。** 检索不做任何访问控制，所有语料对所有人可见。
- **`MemoryStore` 是精确检索，`ChromaStore` 是近似检索。** 原理上两者结果不会完全一致，
  这是 HNSW 的召回损失。但在本项目规模（28 条向量）上实测指标完全相同，
  语料上到几十万条以后才可能拉开差距。
- **解析层很薄。** PDF 只做纯文本提取，不处理双栏、表格结构、扫描件 OCR。
  真实场景建议换成 `unstructured` 或专门的版面分析模型——这一步决定 RAG 的天花板，
  值得单独投入。
