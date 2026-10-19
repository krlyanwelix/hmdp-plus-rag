# 野径 · 赛事助手（RAG 检索与问答服务）

城市路跑 / 越野赛事报名系统里的「赛事助手」：选手在报名前问规程问题（几点关门、要带什么装备、
半马还有没有名额、退赛怎么退），它基于**赛事方真实发布的规程文档**回答，并给出可点开的引用来源。

这个仓库是**检索与问答服务**（Python）。报名主链路（名额预占、报名单状态机、名额对账、鉴权）
在另一个仓库的 Spring Boot 服务里，两者通过 HTTP 对接：

```
  浏览器 ──► Spring Boot 报名服务 ──HTTP──► 本服务（RAG）
                │  /assistant/ask          /v1/ask
                │  /assistant/ask/stream   /v1/ask/stream
                ▼
           MySQL / Redis / RocketMQ
```

**职责边界（很重要）**：问的是"规则"才走这里；问的是"我报名成功了吗 / 还有几个名额"这类
**结构化事实**，由 Java 侧查库后以 `userFacts` 注入，或者干脆不进这里 —— 见
[为什么私有数据不进向量库](#为什么私有数据不进向量库)。

---

## 1. 它解决什么问题

报名系统的咨询量集中在少数几十个问题上，而答案全在组委会那几份 PDF / Markdown 规程里。
直接接大模型会编（"大概 6 小时关门吧"），人工客服又答不过来。所以这里做三件事：

1. **只依据给定文档回答**，每句结论后面带 `[1]` 这样的来源编号，前端能点开原文；
2. **检索不到就明说没收录**，不猜；
3. **挂了不影响报名**：本服务超时或报错时，Java 侧 600ms 内降级成"助手暂时不可用，
   报名与支付不受影响"，主链路照常。

---

## 2. 架构

### 2.1 离线：建索引

```
data/sports/*.md          7 篇本届规程 + 1 篇上届历史版本（故意的干扰项）
      │
      ├─ parse      按 Markdown 标题树解析，保留章节路径
      ├─ split      父块 1500 字（喂给 LLM 的上下文单元）
      │             子块 500 字 / overlap 80（真正参与检索的最小单元）
      │             切分时记录字符偏移，子块继承父块的「真实章节标题」
      ├─ embed      bge-m3（API，非对称检索，查询端加前缀）
      └─ store      Milvus（collection=rag_chunks，COSINE）
                    标量列：source / parent_id / event_id
```

**父子块（small-to-big）**：用小子块去匹配问题（精度高），命中后把它的父块整段喂给模型
（上下文完整）。父块本身不进向量库 —— 进了只会稀释检索结果。

### 2.2 在线：一次提问

```
问题 ──► 赛事过滤 where={event_id:N} ──► 双路召回（各 30 条）
                                          ├─ 向量：bge-m3 + COSINE
                                          └─ BM25：中文分词 + 术语精确匹配
                                                    │
                                              RRF 融合（k=60）
                                                    │
                                          cross-encoder 重排（API）
                                                    │
                                       取父块 + lost-in-the-middle 重排
                                                    │
                                  prompt：强制 [n] 引用 + 只能答文档内 + 拒答约束
                                                    │
                                          LLM（deepseek）→ 引用校验
```

四条工程约束（都在代码里，不是文档承诺）：

| 约束 | 做法 |
|---|---|
| 赛事隔离 | `where={"event_id": n}` 下沉到向量库，在 ANN 阶段过滤，不让别的赛事占满 top_k |
| 用户隔离 | userId 只从 HMAC 签名头取；私有数据以 `userFacts` 注入 prompt，**不进向量库** |
| 缓存 | Redis，key 含 `event_id`/`userId`/语料版本号，`userScoped` 载荷 TTL 60s 且加密 |
| 降级 | 检索空 → 固定拒答；生成挂 → 摊开检索原文；全挂 → 仍返回可展示结果，永不 5xx |

---

## 3. 快速开始

### 3.1 起基础设施（Redis + Milvus）

```bash
docker compose -f deploy/docker-compose.yml up -d
docker ps                      # redis / milvus 都应是 healthy
```

Milvus 官方 compose 要额外挂 etcd 和 MinIO 两个容器；这里用
`ETCD_USE_EMBED=true` + `COMMON_STORAGETYPE=local` 把它们省掉了，只起两个容器。

### 3.2 装依赖

`.pylibs` 里是 **cp312 二进制**，必须用 Python 3.12（3.13/3.14 会报
`_multiarray_umath.cp312_win_amd64.pyd incompatible`）。

```bash
E:\Python312\python.exe -m pip install --target .pylibs -r requirements.txt
cp .env.example .env        # 填 EMBED_API_KEY / RERANK_API_KEY / LLM_API_KEY
```

### 3.3 建索引 + 起服务

```bash
E:\Python312\python.exe cli.py ingest --rebuild    # 8 篇 → 26 子块 / 10 父块
E:\Python312\python.exe service\run.py             # 8090
```

### 3.4 问一句

```bash
E:\Python312\python.exe cli.py ask "50公里组需要带头灯吗，几副电池"
E:\Python312\python.exe scripts\dev_ask.py --event 2 --user 1 "关门时间是几点"
```

---

## 4. 目录

```
rag/                     检索内核（与 HTTP 无关，可单独当库用）
├─ ingest/               解析 + 父子块切分
├─ index/                embed / store（memory|chroma|milvus）/ parents / meta
├─ retrieve/             bm25 / fusion(RRF) / rerank / retriever
├─ generate/             prompt / llm（含流式）
├─ eval/                 metrics（6 个指标 + 负样本）
├─ cache.py              Redis 答案缓存（AES-GCM 加密 + 版本号失效）
├─ ratelimit.py          Redis 令牌桶（fail-open）
├─ pipeline.py           ask / stream 编排
└─ indexer.py            建索引流程

service/                 HTTP 层
├─ api.py                /v1/ask、/v1/ask/stream、/healthz、/v1/stats
├─ security.py           HMAC-SHA256 验签（60s 时间窗）
└─ run.py                先注入 .pylibs 再起 uvicorn

scripts/                 调试与验收脚本
data/sports/             语料（8 篇，含 1 篇上届历史版本作干扰）
eval/dataset-sports.jsonl  评测集（28 题，26 题带证据要点 + 2 题负样本）
deploy/docker-compose.yml   Redis + Milvus
```

---

## 5. 命令行

```bash
python cli.py ingest [--rebuild]    # 建索引（--rebuild 全量重建并 bump 缓存版本号）
python cli.py stats                 # 索引与组件状态
python cli.py search "问题"          # 只检索不生成（调检索时最常用）
python cli.py ask "问题"             # 完整问答 + 引用校验 + token 成本
python cli.py eval --compare        # 三种检索模式各跑一遍，看消融
python cli.py chunks                # 看切块结果（调 chunk_size 时用）
```

`ask` 会额外打印引用校验结果：`越界引用`（模型编了不存在的编号，硬错误）和
`疑似无引用的行`（过渡句可忽略）。

---

## 6. 服务接口

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 索引条数、组件后端（store / cache / rateLimit）、错误信息 |
| `POST /v1/ask` | 非流式问答，返回 `answer` + `citations[]` + `degraded` + `refused` + `cached` |
| `POST /v1/ask/stream` | SSE 流式：先 `meta`（检索结果），再 `delta`（增量文本），最后 `done` |
| `GET /v1/stats` | pipeline / cache / rateLimit 三块统计 |

请求体：

```json
{"question": "50公里组需要带头灯吗", "eventId": 2, "topK": 5, "useCache": true,
 "mode": "hybrid", "userFacts": ["你已报名 E2-50公里组，状态=待支付"]}
```

**内部鉴权**：Java 侧对 `"{user}\n{ts}\n" + sha256(body)` 做 HMAC-SHA256，放进
`X-Rag-User` / `X-Rag-Ts` / `X-Rag-Sign` 三个头。服务端校验 60s 时间窗
（`hmac.compare_digest` 比较）。没配 `RAG_SHARED_SECRET` 时退化为本机调试模式。

**错误语义**（Java 侧据此决定文案）：

| 情况 | 返回 |
|---|---|
| 签名错 / 缺签名 | `401`，Java 侧当"助手不可用" |
| 超过限流 | `429` + `Retry-After`，`refused=true` 但 `degraded=false`（限流不是故障） |
| 检索无命中 | `200` + 固定拒答文案，`refused=true` |
| 生成层异常 | `200` + 检索原文，`degraded=true` |
| 检索层异常 | `200` + 降级文案，`degraded=true` |

---

## 7. 评测

28 道题，26 题标注了"证据要点"（答案必须命中的事实），2 题负样本（问的是文档里**不存在**
的信息，用来测拒答）。k=5。

**线上配置（hybrid + API 重排）**

| recall@5 | 证据召回 | 证据@1 | 证据 MRR | 来源 MRR | nDCG@5 |
|---:|---:|---:|---:|---:|---:|
| 0.929 | 1.000 | 0.846 | 0.910 | 0.815 | 0.845 |

**关掉重排做召回消融**（`RERANKER=none`）

| mode | recall@5 | 证据召回 | 证据@1 | 证据 MRR | 来源 MRR | nDCG@5 |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 0.893 | 1.000 | 0.731 | 0.849 | 0.714 | 0.761 |
| bm25 | 0.929 | 0.923 | 0.731 | 0.811 | 0.789 | 0.824 |
| vector | 0.893 | 1.000 | 0.615 | 0.795 | 0.708 | 0.756 |

三个结论（都来自这张表，不是常识转述）：

1. **重排把证据@1 从 0.731 抬到 0.846，证据 MRR 从 0.849 抬到 0.910** ——
   排序质量确实是重排在赚。
2. **两路召回互补，但互补点不在同一层**：不重排时 bm25 的证据@1（0.731）明显好于
   vector（0.615），而 vector 的证据召回是满的（1.000）、bm25 只有 0.923 —— 一个排得准、
   一个找得全。融合后 hybrid 拿到 1.000 的召回并把证据 MRR 拉到 0.849，但证据@1 仍与 bm25 持平，
   说明**融合补的是"找得全"，"排得准"要交给重排**。所以做召回消融**必须关掉重排**，
   否则重排会把两路的差异抹平。
3. **术语型语料上 BM25 是强基线**：规程里全是"50 公里组""关门时间""强制装备"这种固定术语，
   字面匹配比语义泛化更可靠。向量召回的短板在那种"换个说法问同一件事"的问题上才显现。

> 换个语料就必须重跑。这些数字只对 `data/sports/` 这 8 篇、`eval/dataset-sports.jsonl` 这 28 题成立。

---

## 8. 配置

全部集中在 `config.py`，通过 `.env` 覆盖。常用的：

| 键 | 默认 | 说明 |
|---|---|---|
| `RAW_DIR` | `data/sports` | 语料目录 |
| `STORE_BACKEND` | `auto` | `milvus` / `chroma` / `memory`，auto 按可用性降级 |
| `RAG_MILVUS_URI` | `data/milvus.db` | 独立集群填 `http://127.0.0.1:19530` |
| `EMBEDDER` | `auto` | `openai` 走 API；**`auto` 不会自动用 API**，见踩坑 |
| `RERANKER` | `auto` | `api` 走接口；`none` 关掉（消融必须） |
| `TOP_K_RECALL` / `TOP_K_FINAL` | 30 / 5 | 召回放宽，精度交给重排 |
| `RRF_K` | 60 | RRF 平滑常数 |
| `REDIS_URL` | — | 缓存与限流共用 |
| `RAG_CACHE_TTL` / `RAG_CACHE_USER_TTL` | 300 / 60 | 公开答案 / 含个人信息的答案 |
| `RAG_CACHE_ENCRYPT` | `true` | 缓存值 AES-GCM 加密后再进 Redis |
| `RAG_RATE_LIMIT_PER_MIN` / `_BURST` | 20 / 5 | 按 userId 的令牌桶 |
| `LLM_TIMEOUT` / `LLM_STREAM_TIMEOUT` / `LLM_DEADLINE` | 45 / 20 / 45 | 整体 / 单次读 / 整段墙钟 |
| `RAG_SHARED_SECRET` | 空 | 内部验签密钥，留空=调试模式 |

---

## 9. 几个刻意的设计决定

### 为什么父块不进向量库

父块 1500 字，塞进向量库会让"一个块里什么都有"，相似度被平均掉；子块 500 字匹配准，
但单独喂给模型又缺上下文。所以子块负责"被检索到"，父块负责"被读"。

### 为什么 RRF 而不是加权求和

向量分数（余弦）和 BM25 分数（无界正数）不在一个量纲上，加权就得先归一化，而归一化本身
会引入新的假设。RRF 只用名次：`1/(k+rank)`，两路各 60 名以内按名次融合，不需要调权重。

### 为什么重排走 API 而不是本地模型

本地 bge-reranker-v2-m3 要拉 torch（约 4GB）并常驻显存，冷启动 40s+。这个项目
embedding 和重排都已经走 API，服务启动预热 0.9s，部署不依赖 GPU。

### 为什么私有数据不进向量库

"我的报名"一旦切块入向量库，别人提问也可能把它检索出来。所以：userId 只从签名头来，
用户数据由 Java 查库后以 `userFacts` 注入 prompt，并且 prompt 里明确规定"只能答本人的，
问别人一律回'只能查询你本人的报名信息'"。

### 为什么缓存要加密

Redis 里躺着的是"某个用户问了什么 + 答案"，含手机号、证件号脱敏片段。缓存值用
AES-GCM（key 由 `RAG_CACHE_KEY` 经 HKDF 派生）加密后存，进 Redis 只能看到 `e1:` 前缀的密文。

### 为什么两层限流

网关层（OpenResty 令牌桶）保**全局总量**，应用层（Redis 按 userId 令牌桶）保
**单用户公平**。只做一层的话，要么扛不住总量，要么一个人能把额度吃光。

---

## 10. 踩坑记录

- **`MILVUS_URI` 这个名字不能用**：pymilvus 把它当全局默认连接解析，设成本地文件路径会在
  `import` 阶段直接抛 `ConnectionConfigException: Illegal uri`。项目里用 `RAG_MILVUS_URI`。
- **`EMBEDDER=auto` 不会自动改用 API**：没装 sentence-transformers 时它静默退回内置
  hashing embedder，检索质量悄悄掉一个档，而且日志里只有一行不起眼的提示。纯 API 模式必须显式写 `openai`。
- **Redis 用 Lua 时**：老版本（3.2）`HSET` 的 arity 是 4，一次写两个 field 会报
  `Wrong number of args`；脚本里先调了 `TIME` 再写 Redis 会报
  `Write commands not allowed after non deterministic commands`，必须加 `redis.replicate_commands()`。
- **`python -c "..."` 在 PowerShell 里是引号地狱**：脚本里带 `redis.call(...)` 时参数会被截断，
  一律写成临时 `.py` 再跑。
- **milvus-lite 是单进程独占的**：db 文件被占用时另一个进程打不开（Windows 下 `WinError 5`）。
  多实例共享必须用独立 Milvus（就是 compose 里那个）。

---

## 11. 已知限制

1. **检索内核本身没有并发**：`pipeline` 是同步的，重排走 HTTP，单次 2~3s。要扛并发得靠多进程/多实例。
2. **语料是手工维护的 8 篇**，没有增量同步；换一版规程要重跑 `ingest --rebuild`。
3. **限流只有应用层这一层**（网关那层在另一个仓库的 Nginx 配置里），单实例部署时它就是全部。
4. **生成超时只能断，不能续**：墙钟到点直接切，已生成的文本会保留并标记 `error`。
5. **评测集只有 28 题**，负样本 2 题，统计意义有限；指标变化小于 0.02 时不要当作改进。
6. **拒答判定是关键词启发式**（`_looks_refused`），模型说"我无法回答"的变体可能漏判。

---

## 12. 与 Java 主链路的关系

| 场景 | 走哪边 |
|---|---|
| "50 公里组要带头灯吗" | 本服务（规程检索） |
| "半马报名费多少" | 本服务（组别信息来自规程文档） |
| "我报名成功了吗" | 本服务 + `userFacts`（数据由 Java 查库注入） |
| "还剩几个名额" | **Java 直接查 Redis 余量**，不进本服务（余量是秒级变化的结构化事实） |
| "帮我取消报名" | Java（写操作，需要鉴权与幂等） |

这条边界的意义：让大模型只处理"自然语言的规则解释"，把"结构化事实"和"写操作"留在
确定性的代码里。助手可以挂，报名不能挂。
