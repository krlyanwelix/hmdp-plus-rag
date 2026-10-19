# 设计说明：报名链路的时序、失败矩阵与取舍

## 1. 四个时刻

一定要把"拿到报名号"和"报名成功"分开，否则后面所有讨论都会含混。

| 时刻 | 发生什么 | 系统状态 |
|---|---|---|
| **t1** | `nextId("reg")` 生成报名号 | 什么都没变 |
| **t2** | 半消息发到 Broker（存为对消费者不可见的半消息） | 消费者看不到这条消息 |
| **t3** | `reserve.lua` 执行：余量-1、`SADD` 一人一赛、写 `reg:txn:{id}` | **名额已占**，但库里没有单 |
| **t4** | `endTransaction(COMMIT)` 发出（oneway） → HTTP 响应返回 | 用户拿到报名号 |
| **t5** | Broker 把半消息转正式消息，消费者拉取 | — |
| **t6** | `persist` 提交：DB 扣余量 + 插入报名单 | 库里终于有这条单 |
| **t7** | 支付 / 超时 / 退赛 → 终态 | 名额释放或固化 |

t4 到 t6 之间有一个真实的窗口：**用户已经拿到单号，但 `GET /registration/{id}` 会查不到**。
这不是 bug，是异步落单的必然结果。前端必须按"轮询 result 接口"处理，而不是拿到单号就去查详情。

`endTransaction` 是 **oneway** 调用——客户端不等待 Broker 确认。所以 t4 时连"commit 是否到达 Broker"
都不保证。它丢了怎么办？Broker 发现半消息长时间未确认，会回调 `checkLocalTransaction`，
我们读 `reg:txn:{id}` 决定 COMMIT/ROLLBACK。这就是为什么事务标记必须和扣减写在同一个脚本里。

---

## 2. 失败矩阵

| 失败点 | 后果 | 兜底 |
|---|---|---|
| 发半消息失败（网络/超时） | Lua 未执行，零副作用 | 直接返回"系统繁忙"，用户重试安全 |
| 半消息成功，`executeLocalTransaction` 抛异常 | 可能已扣也可能没扣 | 返回 `UNKNOW` → 回查读 marker → 有则 COMMIT，无则 ROLLBACK |
| Lua 返回 1（余量不足）/ 2（重复报名） | 没扣，脚本本身没产生副作用 | ROLLBACK，接口返回明确文案 |
| COMMIT 丢失 | 半消息一直不回查不到 | Broker 回查 → marker 存在 → COMMIT |
| 消费者消费失败 | 库内无单，Redis 已占位 | 阶梯重试 16 次 → 死信 → 对账"漏单补偿"用 `reg:pending` 重发 |
| `persist` 中 DB 余量不足 | 事务回滚，库内无单 | 回补 Redis（余量+1、SREM、删 marker），然后 ack，不重试（重试也修不了） |
| 撞唯一索引 `(event_id, athlete_id, active_flag)` | 事务回滚 | 同上回补 Redis；这条路径就是"重复报名"的最终裁判 |
| 延迟消息投递失败 | 单子永远停在待支付 | 对账"超时关单兜底"按 `create_time` 扫 |
| 关单时 DB 回补失败 | DB 余量偏少 | 对账"余量重算"以订单表为账本改写 |
| 关单时 Redis 回补失败 | Redis 余量偏少（少卖） | 同上 |
| 支付与关单并发 | 一个成功一个失败 | 双方都是 `WHERE status = 待支付` 的 CAS |
| 退款与关单并发 | 不会双回补 | 退款要求 `status = 已支付`，关单要求 `待支付`，互斥 |
| 应用重启，Redis 余量与 DB 不一致 | 可能多卖/少卖 | `QuotaInitializer` 用 `setIfAbsent` 播种（不覆盖）；对账重算归位 |

**"回补以订单状态为准"是什么意思**：不是"我记得我扣过，所以加回来"，而是
"CAS 把状态从待支付改成已取消**成功了**，才加回来"。CAS 是幂等的，重复执行只会返回 0 行。

---

## 3. 为什么不是别的方案

**为什么不用本地消息表？**
本地消息表要求"业务表 + 消息表"在同一个本地事务里，再由投递线程扫表发送。它的问题是：
名额扣减在 Redis，不在这个事务里，于是又绕回"Redis 扣了但 DB 事务失败"的原点。
事务消息的价值是把"Redis 扣减"这个本地事务和"消息可消费"绑在一起，这正好是我们要的。

**为什么不用 Seata 之类的分布式事务？**
名额是热点，AT 模式会给 `tb_event_group` 加全局锁，等于把并发压成串行。
报名这种"允许最终一致、但不能超卖"的场景，用"预占 + 唯一索引 + 对账"更划算。

**为什么一人一赛用 `(event_id, athlete_id, active_flag)` 而不是 `(event_id, athlete_id)`？**
后者会让退赛的人永远无法重报——因为历史单还在。加上 `active_flag` 后：
取消/退款时置 **NULL**（不是 0），有效单唯一性由索引保证，历史单保留可追溯。

失效值用 NULL 而不是 0 是实测踩出来的：MySQL 唯一索引对 `0` 去重，
同一证件退赛两次就会撞 `uk_event_athlete_active`，接口直接返回"服务器异常"；
而多个 `NULL` 不算重复，所以历史失效单可以无限留痕。

**为什么对账只处理已截止的组别？**
报名进行中的组别，Redis 余量与"有效单数"天然对不上（有人已占位但还没落库）。
这时候重算会把正在处理的单判成"多扣"，直接导致超卖。所以只对时间窗已关的组别做"账本归位"。

**为什么延迟档位是 14（10 分钟）而不是 15（20 分钟）？**
RocketMQ 4.x 默认 18 档里没有 15 分钟这个值，14 档是 10 分钟、15 档是 20 分钟。
对外说"15 分钟关单"而实际延迟 20 分钟，是很多项目口径不一致的根源。这里选择
"10 分钟延迟 + 对账每分钟扫"——关单最迟 11 分钟完成，比"声称 15 分钟实际 20 分钟"更诚实。
要精确 15 分钟，改 `messageDelayLevel` 重启 Broker 即可。

---

## 4. 参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| `registration.timeout-minutes` | 15 | application.yaml / 对账任务 / 前端文案共用 |
| `registration.delay-level` | 14（10 分钟） | 见上 |
| `registration.rate-limit.sliding-window` | 1000ms / 5 次 | 应用层限流 |
| 网关令牌桶 | 1000/s，桶 3000 | openresty/nginx.conf |
| `quota:group:{id}` | 无 TTL | 余量常驻，由对账归位 |
| `reg:txn:{id}` | 3600s | 回查窗口 |
| `reg:pending:{userId}:{eventId}` | 600s | 反查报名号 |

---

## 5. 构建

已验证：

```bash
mvn -o -DskipTests package     # BUILD SUCCESS，55 个源文件
```

本地仓库 `E:\Maven\apache-maven-3.9.11\maven_repo` 里已具备 `caffeine:2.9.3`、
`rocketmq-client:4.9.7`（含 rocketmq-common/remoting/logging）与 `com.mysql:mysql-connector-j:8.0.33`。

注意两点：

1. Maven 本地仓库在 `E:\Maven\...` 下，**不在工程目录内**，构建时需要对该目录有写权限
   （Maven 会写 `_remote.repositories` 等追踪文件）。
2. 事务消息生产者必须用 `TransactionMQProducer`——`setTransactionListener` 与
   `sendMessageInTransaction` 只定义在它上面；并且要 `setExecutorService`，
   否则 Broker 回查时无执行器可用。
