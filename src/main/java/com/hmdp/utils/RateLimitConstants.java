package com.hmdp.utils;

/**
 * 限流相关 Redis key。
 * <p>
 * 第二层（应用内）按账号维度做滑动窗口；第一层（网关）的令牌桶状态在 OpenResty 的
 * {@code lua_shared_dict} 里，不占用 Redis。
 */
public class RateLimitConstants {

    /** rate:sw:register:{userId} */
    public static final String SLIDING_WINDOW_REGISTER_KEY = "rate:sw:register:";

    private RateLimitConstants() {
    }
}
