package com.hmdp.config;

import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.concurrent.TimeUnit;

/**
 * L1 本地缓存。
 * <p>
 * 容量 10000、写入 10 分钟后强制过期：Caffeine 在 JVM 内，Canal 的失效广播是唯一
 * 跨实例手段，expireAfterWrite 是它失效时的最后一道兜底，防止某次广播丢失导致脏数据常驻。
 */
@Configuration
public class CaffeineConfig {

    @Bean("eventLocalCache")
    public Cache<String, Object> eventLocalCache() {
        return Caffeine.newBuilder()
                .initialCapacity(256)
                .maximumSize(10_000)
                .expireAfterWrite(10, TimeUnit.MINUTES)
                .recordStats()
                .build();
    }
}
