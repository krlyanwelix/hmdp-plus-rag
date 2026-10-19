package com.hmdp.utils;

/**
 * RocketMQ 常量。
 * <p>
 * 写链路上 MQ 承担两个不同角色：
 * <ul>
 *   <li>{@link #TAG_CREATE}：事务消息，负责"名额预占"与"报名单落库"的原子性；</li>
 *   <li>{@link #TAG_TIMEOUT}：延迟消息，负责"到点自动关单"，充当定时器。</li>
 * </ul>
 * 两者共用同一个 Topic，消费端按 Tag 分支。
 */
public class RocketMQConstants {

    /** 报名 Topic */
    public static final String REGISTRATION_TOPIC = "registration-topic";
    /** 创建报名单（事务消息） */
    public static final String TAG_CREATE = "CREATE";
    /** 超时关单（延迟消息） */
    public static final String TAG_TIMEOUT = "TIMEOUT";

    /** 事务消息生产者组：Broker 按组回查 */
    public static final String TX_PRODUCER_GROUP = "registration-tx-producer-group";
    /** 普通生产者组：用于方案外的补偿重发（对账补单） */
    public static final String PRODUCER_GROUP = "registration-producer-group";
    /** 消费者组 */
    public static final String CONSUMER_GROUP = "registration-consumer-group";

    /** Canal binlog 广播 Topic（缓存失效） */
    public static final String CANAL_TOPIC = "canal-binlog-topic";
    public static final String CANAL_CONSUMER_GROUP = "canal-cache-consumer-group";

    private RocketMQConstants() {
    }
}
