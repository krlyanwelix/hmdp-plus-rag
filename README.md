# 纯 Python RAG 骨架

一套**零依赖即可跑通**的 RAG 骨架：解析 → 切块 → 向量化 → 混合检索 → 重排 → 生成 → 引用溯源，
外加一个能给出可比较数字的检索评测。

设计取向是**每个环节都可插拔、可观测**，而不是堆一个开箱即用的黑盒。
默认用内置的降级实现跑起来，装上真实组件后自动切换，代码不用改。

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

`data/raw/` 下已放好 6 篇示例文档（虚构的 XR-2000 网关产品资料），
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

**已跑通并验证**
- 全链路 `ingest → search → ask → eval → chunks`
- 增量索引、父子块展开、`lost-in-the-middle` 重排、引用编号映射
- 内置 hashing embedder、BM25、RRF、词法重排
- 评测指标（含对排序敏感的证据 MRR）
- **`ChromaStore`（真实 chromadb 1.5.9 实跑通过）**
- **两个向量后端的结果一致性**

**实测数据**（20 题，k=5，零依赖降级栈，memory 与 chroma 后端结果完全相同）

```
mode        recall@5       证据召回      证据@1     证据MRR     来源MRR    ndcg
hybrid         0.950      1.000     0.895     0.947     0.900   0.909
```

19 道有证据的题里 17 道答案就在第一段，2 道在第二段；1 道负样本不计入。

**关于两个后端的一致性：** `MemoryStore` 是精确检索，`ChromaStore` 是 HNSW 近似检索，
在这个规模（28 条向量）上指标完全一致，说明 ANN 的召回损失可以忽略。
语料上到几十万条以后两者才可能拉开差距，届时以 Chroma 为准。

**仍未实跑**
- `SentenceTransformerEmbedder` / `CrossEncoderReranker`（缺 `sentence-transformers`）
- PDF / HTML 解析分支（缺 `pypdf` / `beautifulsoup4`）

---

## 可选依赖装在哪

不想污染共享 Python 运行时的话，装到工作区本地目录：

```bash
pip install --target .pylibs chromadb sentence-transformers
```

`cli.py` 启动时会自动把 `.pylibs` 追加到 `sys.path`，所以直接
`python cli.py stats` 就能看到它生效，不需要设 `PYTHONPATH`。
该目录已在 `.gitignore` 里。

装完 chromadb 后注意：`STORE_BACKEND=auto` 会**自动从 memory 切到 chroma**，
而新后端的集合是空的。程序会打印明确警告并提示重建，但重建仍然必须由你触发。

### 一个重要的坑：零依赖栈下三种模式分数完全相同

`eval --compare` 在降级栈上会显示 vector / bm25 / hybrid 三个模式**分数一模一样**。
这不是 bug——关掉重排后逐题对比 chunk 顺序，三者确实不同。

原因是：hashing embedder 本质上也是词法模型（字符 n-gram 哈希），
和 BM25、词法重排属于同一类信号，彼此没有互补性，所以融合后排序趋同，
再经过同源的重排就完全抹平了。

**混合检索的价值只有在两路召回真正异质时才体现。** 换上 `bge-m3`（语义）
+ BM25（词面）+ `bge-reranker`（交叉注意力）之后，差异才会显现。

---

## 换成生产组件

```bash
pip install --target .pylibs chromadb sentence-transformers pypdf beautifulsoup4 jieba
```

装完不用改代码，`auto` 会自动切换。然后 `python cli.py ingest --rebuild` 重建索引
（**必须重建**，向量空间变了），再用 `eval` 对比升级前后的指标。

**换后端或换 embedding 模型后不重建，程序会报警。** 索引目录里存了 `index_meta.json`，
记录这份索引用什么建的；检测到不一致时 `search` / `eval` / `stats` 都会打印
明确的迁移提示。这个检查**不受 `-q` 影响**——它是正确性问题，不是日志噪音。

`stats` 命令会显示每个环节实际生效的实现：

```
  embedder          : sentence-transformers
  reranker          : cross-encoder
  store             : chroma
  llm               : openai-compatible
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
