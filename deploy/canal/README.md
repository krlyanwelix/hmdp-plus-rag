# Canal：缓存失效链路（可选组件）

主链路不依赖它：应用写库后会自己删缓存。这条链路解决的是**绕过应用的写**——DBA 手工改库、
别的服务改同一张表、以及多实例部署时别的实例 JVM 里的 Caffeine。

```
MySQL ROW binlog ─► Canal Server（rocketMQ 模式、flatMessage）─► canal-binlog-topic
                                                                      │
                                        CanalCacheEvictConsumer ◄─────┘
                                                  │
                                cache:event:{id} / cache:eventGroup:{eventId}
```

实测：绕过应用改库后，赛事缓存 318ms 失效、组别列表 256ms 失效。

## 1. 前置

```sql
-- MySQL 必须：log_bin=ON、binlog_format=ROW、binlog_row_image=FULL
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'binlog_format';
-- 只给读 + 复制权限，Canal 不需要写权限
CREATE USER 'canal'@'%' IDENTIFIED WITH mysql_native_password BY 'canal123';
GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'canal'@'%';
```

RocketMQ 侧预建 topic（消费者启动时 topic 不存在会空转约 30s）：

```bash
mqadmin.cmd updateTopic -n 127.0.0.1:9876 -c DefaultCluster -t canal-binlog-topic
```

## 走 Docker Compose 时可以跳过这一节

主链路的 `deploy/docker-compose.yml` 已经把 Canal 容器、两个配置文件、只读账号都接好了：

```
deploy/canal/canal.properties    -> 容器 conf/canal.properties   （serverMode=rocketMQ，namesrv=namesrv:9876）
deploy/canal/instance.properties -> 容器 conf/example/instance.properties （master.address=mysql:3306）
deploy/canal/canal-user.sql      -> MySQL 首次初始化时建 canal 账号
```

下面这些步骤是**不用 Docker、把 canal.deployer 直接跑在宿主机上**时的手工流程。

## 2. 装 Canal

下载 [canal.deployer-1.1.7.tar.gz](https://github.com/alibaba/canal/releases/tag/canal-1.1.7)
解压到任意目录（本机是 `E:\canal`），JDK 用 8。

把本目录的两个**宿主机版**文件覆盖进去（`host-` 前缀是为了和容器版区分）：

| 本目录 | 覆盖到 |
|---|---|
| `host-canal.properties` | `conf/canal.properties` |
| `host-instance.properties` | `conf/example/instance.properties` |

两个文件里的地址/账号指向 `127.0.0.1`，换环境时改这几行为主：

- `host-canal.properties`：`rocketmq.namesrv.addr`
- `host-instance.properties`：`canal.instance.master.address`、`dbUsername`、`dbPassword`、`filter.regex`、`canal.mq.topic`

## 3. 起停与排错

```bash
bin/startup.bat          # Windows；Linux 用 bin/startup.sh
bin/stop.bat             # 停止（Linux：bin/stop.sh）
```

日志在 `logs/canal/canal.log`（进程级）与 `logs/example/example.log`（实例级）。启动成功的标志：

```
## the canal server is running now ......
start successful....
---> find start position successfully, ... the next step is binlog dump
```

**两个最常见的坑**：

1. `canal.serverMode` 忘了从 `tcp` 改成 `rocketMQ`：binlog 解析一切正常，日志也不报错，
   但**一条消息都不会进 MQ**。
2. `canal.mq.flatMessage` 关掉：发的是 protobuf 扁平包，消费者按
   `{"table":"tb_event","data":[{"id":1}]}` 解析会拿到 null 并静默跳过。

## 4. 验证

```bash
# 1) 先热缓存
curl http://127.0.0.1:8081/event/2

# 2) 绕过应用直接改库（去掉应用自己 evict 的影响）
mysql -uroot -p -e "UPDATE hmdp_plus.tb_event SET venue='canal-test' WHERE id=2"

# 3) 看 Redis：cache:event:2 应在一秒内消失
redis-cli EXISTS cache:event:2

# 4) 再读，应拿到新值
curl http://127.0.0.1:8081/event/2
```

RocketMQ 侧可以用 `mqadmin topicStatus -t canal-binlog-topic` 看堆积，
`mqadmin consumerProgress -g canal-cache-consumer-group` 看消费位点。
