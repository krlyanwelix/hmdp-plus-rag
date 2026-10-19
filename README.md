# hmdp-plus-rag · 城市路跑赛事报名与名额结算

把「黑马点评」的本地生活场景整体换成**体育赛事报名**，只保留三条真正有价值的链路，
其余（探店/笔记/点赞/关注/签到/Feed/UV）全部删除。

---

## 〇、仓库结构

| 目录 | 内容 | 技术栈 |
|---|---|---|
| 根目录 | **赛事报名主链路**：名额预占、报名单状态机、名额对账、读路径缓存与限流 | Spring Boot 2.3 / MyBatis-Plus / RocketMQ / Redis / Redisson |
| `rag/` | **赛事助手**：规程检索与问答（独立进程，可单独部署） | Python 3.12 / FastAPI / Milvus / Redis / bge-m3 |

两者只通过 HTTP 对接：

```
浏览器 ──► 主链路 :8081 ──/assistant/ask──────────────► 赛事助手 :8090 /v1/ask
                       ──/assistant/ask/stream(SSE)──►            /v1/ask/stream
```

**助手是旁路**：Python 侧没起或超时，主链路 600ms 内降级为"助手暂时不可用，
报名与支付不受影响"，报名链路照常。细节见 [`rag/README.md`](rag/README.md)。

---

## 一、三条链路

### 1. 写路径：RocketMQ 事务消息 + Redis Lua 原子预占

```
POST /registration/{groupId}
   │
   ├─ 1. 网关 OpenResty 令牌桶（全局总量 + 正常连点）
   ├─ 2. 登录拦截器 → 滑窗限流（按账号）
   ├─ 3. 资格校验（实名档案存在、未被禁赛、组别在报名时间窗内）
   ├─ 4. nextId("reg") → 报名号       ← 此时就已经产生单号
   ├─ 5. 写 reg:pending:{userId}:{eventId}（受理中，10 分钟）
   ├─ 6. 发半消息
   ├─ 7. executeLocalTransaction → reserve.lua（余量-1 / 一人一赛 / 写事务标记，原子）
   ├─ 8. 0 → COMMIT 并返回报名号 ｜ 1/2 → ROLLBACK 并返回失败 ｜ 异常 → UNKNOW
   └─ 9. 返回（约 10–30ms）
        │
        └─ 异步：CREATE 消息 → DB 扣余量 + 插单（同一事务）→ 撞唯一索引则回补 Redis
                 → 事务提交后投递 TIMEOUT 延迟消息
```

**为什么用事务消息而不是"先扣库存再发消息"**：扣减与"消息一定可被消费"必须绑成一件原子的事。
否则扣完名额、消息丢了，名额就永久虚耗，而且没有任何人能发现。

**为什么用 Lua 而不是分段命令**：`GET → SISMEMBER → DECR → SADD → SET` 五步之间任何一次失败，
都会留下无法判定的中间态，Broker 回查时无法回答"本地事务到底成没成"。所以五步写在一个脚本里，
marker 和扣减要么都在、要么都不在。

### 2. 报名单状态机与对账

- 状态迁移全部用 **CAS 条件更新**（`WHERE status = 待支付`），支付与超时关单并发时只有一个能成功。
- 报名成功后投递 **RocketMQ 延迟消息**，到点未支付则关单并回补（DB + Redis）。
- 回补**以订单状态为准**：CAS 成功才 `quota + 1`，杜绝重复回流。
- 主路径之外，`RegistrationReconcileTask` 每分钟在 Redisson 锁下跑三段对账：
  1. **超时关单兜底**：延迟消息丢了/进死信，按 `create_time` 扫出来关掉；
  2. **漏单补偿**：Redis 已预占但库内无有效单 → 用 `reg:pending:*` 反查报名号，重发普通 CREATE 消息；
  3. **余量重算**：`expected = initial_quota − 有效单数`，同时改写 Redis 与 MySQL。

**关键**：对账只对**已过报名截止时间**的组别重算，否则会和正在进行的报名互相覆盖。
`initial_quota` 单独存一列且不可变——没有它就只能"拿余量推余量"，越推越离谱。

### 3. 读路径与分层限流

```
OpenResty lua_shared_dict ──→ Caffeine(10_000, expireAfterWrite 10min)
        ──→ Redis 逻辑过期（±20% 抖动，异步重建，重建期返回旧值）
        ──→ MySQL
```

- **逻辑过期而非物理过期**：命中过期数据时返回旧值 + 后台重建，杜绝缓存击穿。
- **重建互斥**：只有抢到锁的线程重建，其余直接拿旧值；未抢到锁的分支**绝不触碰锁**。
- **多实例一致**：写库后主动删缓存 + Canal 订阅 binlog 广播失效（Caffeine 在 JVM 内，没有广播就是脏数据）。
- **余量不进缓存**：`quota:group:{id}` 每次实时读 Redis。缓存只存赛事静态信息，组别列表只缓存 60 秒。

限流分两层，两层目标不同，所以**没有做成两级令牌桶**：

| 层 | 手段 | 目标 |
|---|---|---|
| 网关 | OpenResty 共享字典令牌桶（1000/s，桶 3000） | 挡全局总量、吸收正常连点突发 |
| 应用 | Redis ZSet 滑动窗口（1000ms / 5 次，按 userId） | 挡单账号脚本刷，避免一人打穿 |

令牌桶允许攒桶（空闲期攒满后一次打穿），滑动窗口没有这个缺陷；反过来滑窗是 O(maxReq) 内存、
不允许突发。两者的短板正好互补。

---

## 二、目录结构

```
hmdp-plus-rag
├─ src/main/java/com/hmdp
│  ├─ HmdpPlusApplication.java      启动类（@EnableScheduling）
│  ├─ config/                       MvcConfig / RedissonConfig / MybatisConfig
│  │                                CaffeineConfig / QuotaInitializer / WebExceptionAdvice
│  ├─ controller/                   User / Athlete / Event / Registration
│  ├─ dto/                          Result / UserDTO / LoginFormDTO / RegistrationMessage
│  ├─ entity/                       User / Athlete / Event / EventGroup / Registration
│  ├─ mapper/                       对应 5 个 BaseMapper
│  ├─ mq/                           RocketMQProducer / RegistrationTransactionListener
│  │                                RegistrationMQConsumer / CanalCacheEvictConsumer
│  ├─ service/ + impl/              4 个业务服务
│  ├─ task/                         RegistrationReconcileTask（三段对账）
│  └─ utils/                        RedisConstants / RedisIdWorker / UserHolder
│                                   MultiLevelCacheService / SlidingWindow*
├─ src/main/resources
│  ├─ application.yaml
│  ├─ db/schema.sql, db/data.sql
│  ├─ lua/reserve.lua, lua/sliding_window.lua
│  └─ openresty/nginx.conf, openresty/lua/token_bucket.lua
└─ docs/design-notes.md, docs/rag-extension.md
```

---

## 三、跑起来

### 前置

| 组件 | 版本 | 必需 |
|---|---|---|
| JDK | 8 | 是 |
| MySQL | 5.7 / 8.0 | 是 |
| Redis | 5.0+ | 是 |
| RocketMQ | 4.9.x（NameServer + Broker） | 是（事务消息/延迟消息） |
| Canal | 1.1.7 | 否（缺省时退化为"只删本地缓存"，跨实例失效失效；配置见下文「缓存失效链路」） |
| OpenResty | 1.21+ | 否（不启动也能直连 8081 压测应用层） |

### 步骤

```bash
# 1. 建库 + 演示数据（注意显式指定 utf8mb4，否则中文会乱码成 ??）
mysql -uroot -p --default-character-set=utf8mb4 -e "source src/main/resources/db/schema.sql"
mysql -uroot -p --default-character-set=utf8mb4 -e "source src/main/resources/db/data.sql"

# 2. 起 RocketMQ（Windows 下是 .cmd，Linux 去掉后缀即可）
set JAVA_HOME=E:\Java-jdks\jdk-8
cd %ROCKETMQ_HOME%\bin
start mqnamesrv.cmd
start mqbroker.cmd -n 127.0.0.1:9876 -c conf\broker-local.conf

# 2.1 建议预建 topic：消费者启动时若 topic 还不存在，要等客户端 30s 路由刷新才会真正订阅上
mqadmin.cmd updateTopic -n 127.0.0.1:9876 -c DefaultCluster -t registration-topic
mqadmin.cmd updateTopic -n 127.0.0.1:9876 -c DefaultCluster -t canal-binlog-topic

# 3. 启动应用（-Dfile.encoding=UTF-8 在 PowerShell 下必须整体加引号）
java "-Dfile.encoding=UTF-8" -jar target/hmdp-plus-rag-0.0.1-SNAPSHOT.jar
```

`broker-local.conf` 至少要这几行，否则客户端拿到的是内网/容器 IP：

```properties
namesrvAddr=127.0.0.1:9876
brokerIP1=127.0.0.1
listenPort=10911
autoCreateTopicEnable=true
autoCreateSubscriptionGroup=true
```

### 缓存失效链路（Canal → RocketMQ → 应用）

应用自己写库后会主动删缓存，这条链路解决的是**绕过应用的写**：DBA 手工改库、别的服务改同一张表、
以及多实例部署时**别的实例**的 Caffeine（JVM 内，只有广播能碰到）。

```
MySQL ROW binlog ─► Canal Server（rocketMQ 模式、flatMessage）─► topic canal-binlog-topic
                                                                        │
                                          CanalCacheEvictConsumer ◄──────┘
                                                    │
                                  cache:event:{id} / cache:eventGroup:{eventId} 失效
```

前置条件（缺一不可，都验证过）：

1. MySQL 开 `log_bin=ON`、`binlog_format=ROW`、`binlog_row_image=FULL`；
2. 一个只有读+复制权限的账号：
   `GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'canal'@'%';`
3. Kafka/RocketMQ 侧 topic 已存在（`mqadmin updateTopic ... -t canal-binlog-topic`）。

Canal 1.1.7 的关键配置（`conf/canal.properties` 与 `conf/example/instance.properties`）：

```properties
# canal.properties：走 MQ 而不是 tcp，且必须是平铺 JSON（消费者按 table/data 解析）
canal.serverMode = rocketMQ
rocketmq.namesrv.addr = 127.0.0.1:9876
rocketmq.producer.group = canal-producer
canal.mq.flatMessage = true
```

```properties
# instance.properties
canal.instance.master.address=127.0.0.1:3306
canal.instance.dbUsername=canal
canal.instance.dbPassword=canal123
# 只订阅会被改的两张表；报名单改动频繁且不缓存，进这条链路只是浪费
canal.instance.filter.regex=hmdp_plus\\.tb_event,hmdp_plus\\.tb_event_group
canal.mq.topic=canal-binlog-topic
```

启动：`bin/startup.bat`（Windows）/ `bin/startup.sh`（Linux），日志在 `logs/example/example.log`，
启动成功的标志是 `start successful....` 与 `the next step is binlog dump`。

> 两个容易卡住的点：`canal.serverMode` 忘了从 `tcp` 改成 `rocketMQ` 时，binlog 解析一切正常但
> **一条消息都不会发**；`canal.mq.flatMessage` 关掉则发的是 protobuf 扁平包，消费者按
> `{"table":..,"data":[..]}` 解析会拿到 null 并静默跳过。

关键配置都在 `src/main/resources/application.yaml`：

| 键 | 默认 | 说明 |
|---|---|---|
| `registration.timeout-minutes` | 15 | 报名单超时时长（只用于对账兜底） |
| `registration.delay-level` | 14 | 14 档=10 分钟、15 档=20 分钟，档位是离散的 |
| `registration.rate-limit.sliding-window.*` | 1000ms / 5 次 | 单用户滑动窗口，只拦 `/registration/**` |
| `rocketmq.name-server` | 127.0.0.1:9876 | 事务消息/延迟消息共用 |

演示账号：手机号 `13800000000`，验证码看控制台日志（`发送短信验证码成功，验证码：xxxxxx`）。
首次报名前要先 `POST /athlete` 补实名档案，否则会被准入校验挡掉。

一键冒烟（需先取到验证码）：

```powershell
powershell -ExecutionPolicy Bypass -File docs/smoke-test.ps1 -Code 123456
# 或复用会话反复跑
powershell -ExecutionPolicy Bypass -File docs/smoke-test.ps1 -Token <token>
```

演示账号：手机号 `13800000000`，验证码看控制台日志（`发送短信验证码成功，验证码：xxxxxx`）。
首次报名前要先 `POST /athlete` 补实名档案，否则会被准入校验挡掉。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/user/code?phone=` | 发验证码（打日志） |
| POST | `/user/login` | `{phone, code}` → token |
| POST | `/athlete` | 保存实名档案（姓名 + 证件号） |
| GET | `/event/list?page=` | 报名中赛事分页 |
| GET | `/event/{id}/detail` | 赛事 + 组别（余量实时） |
| GET | `/event/{id}/groups` | 组别列表 |
| **POST** | **`/registration/{groupId}`** | **抢名额 → 返回报名号** |
| GET | `/registration/result/{id}` | 结果查询（`PROCESSING` 或终态） |
| PUT | `/registration/pay/{id}` | 支付（CAS） |
| PUT | `/registration/cancel/{id}` | 退赛（关单/退款 + 回补） |
| GET | `/registration/my` | 我的报名单（有效单） |
| POST | `/assistant/ask` | 赛事助手（一次性返回，带 `cached`/`refused`/`degraded`） |
| GET | `/assistant/ask/stream?question=&eventId=` | 赛事助手流式（SSE：`meta` → `delta`* → `done`，EventSource 只支持 GET） |
| GET | `/assistant/health` | 助手探活 |

---

## 五、已知限制

1. **支付是假的**：`pay` 只做状态流转，没有对接支付渠道，也没有异步回调与对账。
2. **Canal 只在单机上验证过**：本机跑的是 canal.deployer 1.1.7（JVM 进程，非容器），
   `canal.properties` 用 file 模式持久化位点。多实例/multi-destination、HA 与位点重置都未验证；
   单机部署且"写后删缓存"已生效时，这条链路属于兜底而非必需。
3. **对账是单机串行**：Redisson 锁保证只有一个实例在跑，代价是数据量大时一轮扫不完；
   生产上应按 event_id 分片并发。
4. **没有压测数据**：本机没有 JMeter 环境的跑分一律不写进 README，避免出现无法复现的数字。
7. **`quota:group:{id}` 是唯一真相之外的第二份账**：Lua 里 `GET` 到 nil 会直接判"名额已满"，
   而 `QuotaInitializer` 只在启动时用 `setIfAbsent` 播种。手工 `DEL` 掉这个 key 必须重启应用
   或重新 `SET`，否则该组别会一直显示售罄。生产上应由"对账重算"覆盖这个场景（当前重算只处理已结束的组别）。
8. **消费者启动早于 topic 创建时会静默空转**：客户端要等 30s 一轮的路由刷新才会订阅上，
   期间报名会停在 `PROCESSING`，靠对账补单收敛。部署时先建 topic 可以消掉这个窗口。

---

## 六、构建状态

已用 `mvn -o -DskipTests package` 验证通过，产物 `target/hmdp-plus-rag-0.0.1-SNAPSHOT.jar`（约 61MB）。

依赖已装入本地仓库（`E:\Maven\apache-maven-3.9.11\maven_repo`）：
`caffeine:2.9.3`、`rocketmq-client:4.9.7`、`com.mysql:mysql-connector-j:8.0.33`。

一个当初踩过的坑记在这里：**事务消息的生产者必须用 `TransactionMQProducer`**，
`setTransactionListener` 和 `sendMessageInTransaction` 都只定义在它上面，
`DefaultMQProducer` 没有这两个方法；另外要显式 `setExecutorService`，否则 Broker 回查时没有线程池可用。

---

## 七、实测验证

环境：JDK 8 / MySQL 8.0 / Redis（本机 6379）/ RocketMQ 4.9.7（NameServer + Broker 单机）+ 应用 8081。

| # | 场景 | 操作 | 结果 |
|---|---|---|---|
| 1 | 写路径落单 | `POST /registration/1` | 返回报名号；几十秒后 DB 出现 `status=1` 的单，`quota` 3000→2999，Redis 余量同步 2999 |
| 2 | 超时关单 | `--registration.delay-level=1`（5 秒档）报名后等 9 秒 | `status=4`、`cancel_time` 落库、余量回补 3000、`reg:event:{id}` 集合移除 |
| 3 | 支付与关单竞争 | 报名后 300ms 内支付，再等超时消息到达 | 保持 `status=2`：`closeTimeoutTx` 的 CAS 条件 `WHERE status=1` 没命中 |
| 4 | 退赛后重报 | 取消 → 同赛事换组别重报 → 再取消 | 三次都成功，同一 `(event_id, athlete_id)` 留下多条 `active_flag IS NULL` 的历史单 |
| 5 | 名额售罄 | 把 `quota:group:{id}` 置 0 后报名 | 返回「该组别名额已满」，不产生订单、不写 DB |
| 6 | 重复报名 | 持有效单时再报同一赛事 | 返回「您在本赛事已有报名，请勿重复提交」（Lua 第 2 步 `SISMEMBER` 拦截） |
| 7 | DB/Redis 对账 | 对账任务跑完一轮后比对 | DB `quota=2999` = Redis `2999` = `initial_quota 3000 − 1 张有效单` |
| 8 | 滑动窗口限流 | 1 秒内对 `/registration/**` 连打 6 次 | 第 6 次返回「操作过于频繁，请稍后再试」 |
| 9 | Canal 失效赛事缓存 | 先 `GET /event/2` 热缓存，再 `UPDATE tb_event SET venue=... WHERE id=2`（**绕过应用**） | `cache:event:2` 在 **318ms** 后被删，再次 GET 返回新值；不重启、不改应用代码 |
| 10 | Canal 失效组别列表 | 同样先热 `GET /event/2/groups`，再直接改 `tb_event_group.name` | `cache:eventGroup:2` 在 **256ms** 后失效，组别名立即更新（余量仍走 Redis 实时读） |

---

## 八、赛事助手（RAG 旁路）

### 为什么是"旁路"

报名链路要的是**确定性与低延迟**：预占、落单、关单、对账，每一步都能用状态机和 CAS 证明。
而"资格怎么算、关门几点、强制装备要带什么"这类问题，答案是**非结构化文本**，写进 `if/else` 里维护不动、
交给大模型又会编。所以拆成一条独立路径：

```
用户 ──▶ /assistant/ask ──▶ RagClient ──HTTP──▶ Python RAG 服务（8090）
                                   │                    │
                                   │              检索 → 重排 → 生成
                                   │                    │
                             失败/超时 → degraded=true + 固定话术（不影响报名）
```

四条硬规矩：

1. **助手挂了不影响报名**。`RagClient` 任何异常都返回 `degraded=true` 的可展示对象，不抛 500。
   实测：把 Python 服务停掉，`POST /assistant/ask` 仍在 **552ms** 返回降级话术。
2. **答不出来就说没收录**。检索为空直接回固定拒答文案，不把问题丢给模型去编。
3. **按赛事隔离知识源**。`eventId` 下沉成向量库 metadata `where={"event_id": N}`，
   检索阶段就只在该赛事的文档里找（不是先召回 30 条再过滤），越野赛的规程不会被拿去回答半马的提问。
4. **用户维度隔离**。用户私有数据（报名单、实名档案）**不进向量库**——一旦入库，别人的提问就可能把它召回来。
   做法是按会话身份查库、按请求注入 prompt，并且 `userId` 只能来自 Java 会话，
   用 HMAC 签名传给 Python（`RAG_SHARED_SECRET`），浏览器改不动。

### 代码位置

| 侧 | 文件 | 作用 |
|---|---|---|
| Java | `src/main/java/com/hmdp/client/RagClient.java` | RestTemplate 客户端：500ms 连接超时 / 8s 读超时 / 60s 流式超时，不重试，异常即降级；负责 HMAC 签名与 SSE 解析 |
| Java | `src/main/java/com/hmdp/controller/AssistantController.java` | `POST /assistant/ask`、`GET /assistant/ask/stream`（SSE）、`GET /assistant/health` |
| Java | `src/main/java/com/hmdp/service/impl/AssistantFactsService.java` | 查当前用户的报名单与实名档案，整理成 prompt 事实并脱敏（张三→张*，身份证留首尾 4 位） |
| Java | `src/main/java/com/hmdp/dto/AssistantAskDTO.java` / `AssistantAnswerDTO.java` | 出入参，`degraded`/`reason`/`refused`/`cached`/`userScoped` 是语义字段 |
| Python | `rag/service/api.py` | FastAPI：预热、`where` 过滤、Redis 缓存、按用户限流、拒答、降级、验签、SSE |
| Python | `rag/service/security.py` | `sign` / `verify`：HMAC-SHA256(secret, `"{user}\n{ts}\n"` + sha256(body))，60s 时间窗 |
| Python | `rag/cache.py` | 跨实例答案缓存：Redis + **AES-GCM 静态加密** + 命名空间版本号（`rag:corpus:version`，重建索引时 INCR 整体失效），失败自动退回进程内 LRU |
| Python | `rag/ratelimit.py` | 按 userId 的**令牌桶**（Redis Lua），超限 429 + `Retry-After`；Redis 不可用时 fail-open |
| Python | `rag/index/milvus_store.py` | Milvus 后端（本地 milvus-lite 或远程 19530），`event_id` 走标量字段，过滤条件下推到检索 |
| Python | `scripts/dev_ask.py` | 带签名的调试客户端，支持 `--event/--user/--fact/--no-stream/--bad-sign` |
| 语料 | `rag/data/sports/*.md` | 2 场赛事 8 篇规程（含 1 篇 2025 届历史干扰文档） |
| 评测 | `rag/eval/dataset-sports.jsonl` | 28 题（26 题带证据要点 + 2 题负样本） |

### 跑起来

```bash
# 1. Python 3.12（.pylibs 是 cp312 二进制，3.14 会报 _multiarray_umath 不兼容）
E:\Python312\python.exe -V          # Python 3.12.10

# 2. 建索引（首次或语料变更后）
cd E:\dsh_workspace\pro1\rag
E:\Python312\python.exe cli.py ingest --rebuild

# 3. 起服务（8090）
E:\Python312\python.exe service\run.py

# 4. 起 Java（8081），/assistant/** 需要登录态
```

配置在 `application.yaml`：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `assistant.rag.enabled` | `true` | 置 false 可整体关闭助手入口 |
| `assistant.rag.base-url` | `http://127.0.0.1:8090` | Python 服务地址 |
| `assistant.rag.connect-timeout-ms` | `500` | 同机部署，连不上就是没起 |
| `assistant.rag.read-timeout-ms` | `8000` | 检索+重排+生成三段串行，实测 P99 约 3.3s |
| `assistant.rag.stream-timeout-ms` | `60000` | 流式要等模型吐完，读超时必须放宽 |
| `assistant.rag.shared-secret` | `${RAG_SHARED_SECRET:...}` | 必须与 Python 侧 `.env` 的 `RAG_SHARED_SECRET` 一致；配了才校验签名 |

Python 侧（`rag/.env`）：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `REDIS_URL` | 空（退回进程内缓存） | 例如 `redis://127.0.0.1:6379/0` |
| `RAG_CACHE_ENABLED` | `true` | 关掉便于压测真实检索耗时 |
| `RAG_CACHE_TTL` | `300` | 普通答案缓存秒数，key 里带赛事与用户维度 |
| `RAG_CACHE_USER_TTL` | `60` | **带个人报名数据**的答案单独压短，减少私有数据驻留时间 |
| `RAG_CACHE_ENCRYPT` | `true` | AES-GCM 加密后落 Redis（前缀 `e1:`），key 由 `RAG_CACHE_KEY` HKDF 派生 |
| `RAG_RATE_LIMIT_PER_MIN` / `_BURST` | `20` / `5` | 按 userId 的令牌桶：每分钟 20 次，允许攒 5 次突发 |
| `STORE_BACKEND` | `milvus` | `milvus` / `chroma` / `memory`，`auto` 时按三者顺序降级 |
| `RAG_MILVUS_URI` | `data/milvus.db` | 本地文件即嵌入式 milvus-lite；`http://host:19530` 连独立集群。**不要用 `MILVUS_URI` 这个名字**，它被 pymilvus 当全局默认连接解析 |
| `LLM_TIMEOUT` / `LLM_STREAM_TIMEOUT` | `45` / `20` | 非流式单次连接、流式首个分片的 socket 超时 |
| `LLM_DEADLINE` | `45` | 生成阶段**墙钟**上限：socket 超时只能兜"一个字节都不来"，兜不住"每秒来一个字" |
| `RAG_SHARED_SECRET` | 空（dev 放行） | 配了之后所有 `/v1/*` 调用必须带签名 |

### 实测

| 项 | 结果 |
|---|---|
| `/v1/ask`（直达 Python） | 2.0~3.3s，回答带 `[n]` 引用，引用可回溯到具体文档与章节 |
| 命中缓存 | `cached=true`，`latencyMs=0` |
| **跨实例缓存** | A 实例写入的答案，B 实例（另一进程）直接命中：`cached=true, latencyMs=0` |
| **流式首字** | `GET /assistant/ask/stream`，先推 `meta`（引用卡片可立即渲染），再逐段 `delta` |
| **delta 合批** | 模型 delta 粒度是 1~2 字，服务端按"首段立即发 + 之后 12 字或 60ms 合批"，同一答案从 ~500 个事件降到 **15 个** |
| **用户维度隔离** | 用户 1 问"我报名成功了吗"→ 报出自己那张已支付单；用户 2（无档案无报名）→ 明确答"你还没有报名成功"；用户 2 追问用户 1 的单号 → 拒绝并说明只能查本人 |
| **签名校验** | 错签名 / 无签名 → `401 内部调用身份校验失败`；Java 侧自动带签，浏览器无法伪造 userId |
| 跨赛事提问 | 只命中该赛事文档，模型明确答"资料中没有该项目" |
| 历史版本干扰 | 问到 2026 届规则时能区分 2025 届历史文档并说明"以 2026 届为准" |
| Java 侧降级 | Python 停掉后 552ms 返回 `degraded=true`，`success=true` |
| **按用户限流（Python）** | 用户 9 连打 8 次：第 6 次起 `HTTP 429 {"detail":"提问太快了，请稍后再试"}`，`Retry-After` 约 3s（20 次/分钟 → 每 3s 补 1 个令牌） |
| **按用户限流（过 Java）** | 第 7 次起 `reason=rate_limited`、`refused=true`、**`degraded=false`**，回答"你问得有点快，请 3 秒后再试"——限流不是故障，不能混进降级 |
| **缓存加密** | Redis 里取出来是 `e1:` 前缀密文（`e1:ZZd8iDtg1pryW...`），AES-GCM 解回原文一致；旧明文 key 仍可读，滚动升级不会全量穿透 |
| **私有答案短 TTL** | 带报名数据的答案在 Redis 里 `TTL=60`，普通答案 `TTL=300` |
| **生成超时中断** | 假 LLM 每 0.4s 吐 1 字、`LLM_DEADLINE=2`：2.4s 被主动切断，`error` 事件 + 原文兜底，实际只吐了 6 个字（没被拖死） |
| **Milvus 后端** | `store=milvus`，26 子块 / 10 父块；`event_id` 作为标量字段下推过滤，回答与 chroma 时期一致（头灯那道题仍能区分 2025 届历史版本） |

### 检索评测（28 题，自有语料）

`RERANKER=api`（线上配置）与 `RERANKER=none`（消融）两组，k=5：

| 配置 | recall@5 | 证据召回 | 证据@1 | 证据MRR | 来源MRR | ndcg |
|---|---|---|---|---|---|---|
| hybrid + bge-reranker | 0.929 | 1.000 | **0.846** | **0.923** | **0.815** | **0.845** |
| hybrid（不重排） | 0.929 | 1.000 | 0.769 | 0.885 | 0.723 | 0.776 |
| bm25（不重排） | 0.929 | 1.000 | 0.808 | 0.878 | 0.789 | 0.824 |
| vector（不重排） | 0.893 | 1.000 | 0.769 | 0.885 | 0.708 | 0.756 |

三个结论：

1. **重排确实值钱**：证据@1 从 0.769 拉到 0.846，ndcg 从 0.776 拉到 0.845。
2. **重排会抹平召回差异**：不重排时 bm25 的证据@1（0.808）明显高于 vector（0.769），
   加上重排后两者都被拉到 0.846 —— 所以**做召回消融必须关掉重排**，否则结论是假的。
3. **术语型语料上 BM25 不输向量**：规程里全是"关门时间/强制装备/ITRA 积分"这类精确词，
   子块又短，词法匹配很占便宜。

### 已知限制（RAG 侧）

1. **流式不支持"中途改口"**。已经推给前端的 delta 收不回来，生成阶段挂掉只能补一段原文兜底
   （`error` 事件 + `done.llmError`），前端要能接受"半句话 + 原文"。
2. **缓存仍是"答案级"缓存，key 粒度粗**。同一用户换个问法就是一次冷启动；缓存里确实存着
   答案原文，已做 AES-GCM 加密 + 私有答案 60s 短 TTL，但如果要更彻底，应改成只缓存"引用 id 列表"。
3. **限流只有单层**。Python 侧令牌桶按 userId 限速（跨实例共享 Redis 计数），但没做"全局总量"
   兜底：所有用户一起打满时，压力会直接落到 embedding/rerank 的第三方配额上。
4. **超时只能"断"，不能"续"**。`LLM_DEADLINE` 到点就切断并兜原文，已经生成的部分不会续写；
   要续写得引入会话/草稿，成本远大于收益。
5. **语料是手工整理的**。真实场景要从赛事管理系统导出 + 人工校对，解析质量决定 RAG 天花板。
