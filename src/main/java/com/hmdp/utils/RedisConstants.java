package com.hmdp.utils;

/**
 * Redis key 与状态常量。
 * <p>
 * key 命名统一遵守 {@code 业务:对象:{id}} 的习惯，冒号分隔，便于 SCAN 与按前缀清理。
 */
public class RedisConstants {

    // ==================== 登录会话 ====================
    public static final String LOGIN_CODE_KEY = "login:code:";
    public static final Long LOGIN_CODE_TTL = 2L;

    public static final String LOGIN_USER_KEY = "login:token:";
    public static final Long LOGIN_USER_TTL = 30L;

    // ==================== 缓存 ====================
    /** 空值占位的短 TTL（分钟），用于挡住"查不存在的赛事"打穿数据库 */
    public static final Long CACHE_NULL_TTL = 2L;

    public static final String CACHE_EVENT_KEY = "cache:event:";
    public static final Long CACHE_EVENT_TTL = 30L;

    public static final String CACHE_EVENT_GROUP_KEY = "cache:eventGroup:";
    public static final Long CACHE_EVENT_GROUP_TTL = 10L;

    /** 缓存重建互斥锁：lock:event:{id} */
    public static final String LOCK_EVENT_KEY = "lock:event:";
    public static final Long LOCK_EVENT_TTL = 10L;

    // ==================== 名额与报名 ====================
    /** 组别余量：quota:group:{groupId} —— 余量是热点，只走 Redis，不进多级缓存 */
    public static final String QUOTA_KEY = "quota:group:";

    /** 一人一赛集合：reg:event:{eventId}，value 为 userId */
    public static final String REGISTERED_KEY = "reg:event:";

    /**
     * 事务标记：reg:txn:{registrationId}。
     * 与"扣余量 + 记一人一赛"写在同一个 Lua 里，保证原子；Broker 回查只读它，不重跑脚本。
     */
    public static final String REG_TXN_KEY = "reg:txn:";
    public static final long REG_TXN_TTL_SECONDS = 3600L;

    /**
     * 受理中标记：reg:pending:{userId}:{eventId}。
     * 用于 UNKNOW 分支与客户端超时后反查报名号，避免"占了名额但用户不知道单号"。
     */
    public static final String REG_PENDING_KEY = "reg:pending:";
    public static final long REG_PENDING_TTL_MINUTES = 10L;

    // ==================== 报名单状态 ====================
    /** 1 待支付 */
    public static final int REG_STATUS_UNPAID = 1;
    /** 2 已支付 */
    public static final int REG_STATUS_PAID = 2;
    /** 4 已取消（超时关单 / 主动退赛） */
    public static final int REG_STATUS_CANCELLED = 4;
    /** 6 已退款 */
    public static final int REG_STATUS_REFUNDED = 6;

    /** 有效单标记：唯一索引 (event_id, athlete_id, active_flag) 的第三列 */
    public static final int ACTIVE_FLAG_YES = 1;
    /**
     * 失效单标记：必须是 NULL，不能是 0。
     * <p>
     * MySQL 的唯一索引不认为多个 NULL 互相重复，所以同一证件可以留着任意多张
     * 历史失效单；若用 0，第一次退赛后再退一次就会撞 uk_event_athlete_active
     * （实测：第二次取消直接抛 DuplicateKeyException，接口返回"服务器异常"）。
     */
    public static final Integer ACTIVE_FLAG_NO = null;

    private RedisConstants() {
    }
}
