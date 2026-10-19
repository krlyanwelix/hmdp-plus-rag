package com.hmdp.utils;

import cn.hutool.core.lang.UUID;
import cn.hutool.core.util.BooleanUtil;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import com.github.benmanes.caffeine.cache.Cache;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import javax.annotation.Resource;
import java.time.LocalDateTime;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

/**
 * 多级缓存：Caffeine(L1) → Redis 逻辑过期(L2) → MySQL(L3)。
 * <p>
 * 三个设计点：
 * <ol>
 *   <li><b>逻辑过期而非物理过期</b>：过期时间写在 value 里，读到过期数据时返回旧值并异步重建，
 *       请求不会被阻塞，也不会同时打穿到 DB；Redis 侧另给一个更长的物理 TTL 兜底回收。</li>
 *   <li><b>重建互斥</b>：只有抢到 {@code lock:event:{id}} 的线程去重建，其余线程直接拿旧值。
 *       锁只在抢到它、且确实要重建时释放，未抢到锁的分支绝不触碰锁。</li>
 *   <li><b>空值占位</b>：DB 查不到也写一个短 TTL 的空串，防止"查不存在的赛事"反复穿透。</li>
 * </ol>
 */
@Slf4j
@Component
public class MultiLevelCacheService {

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource(name = "eventLocalCache")
    private Cache<String, Object> localCache;

    private static final ExecutorService CACHE_REBUILD_EXECUTOR = Executors.newFixedThreadPool(10);

    /**
     * 物理 TTL 比逻辑过期多留的时间。
     * 逻辑过期后还有一段时间可以读到旧值、异步重建；物理 TTL 只是最后的回收兜底。
     */
    private static final long PHYSICAL_TTL_GRACE_MILLIS = TimeUnit.MINUTES.toMillis(10);

    /**
     * 读缓存，未命中则回源。
     *
     * @param keyPrefix  缓存 key 前缀
     * @param id         业务主键
     * @param type       返回类型
     * @param dbFallback DB 回源函数
     * @param time       逻辑过期时长
     * @param unit       时间单位
     */
    public <R, ID> R queryWithMultiLevel(String keyPrefix, ID id, Class<R> type,
                                         Function<ID, R> dbFallback, Long time, TimeUnit unit) {
        String key = keyPrefix + id;

        // ---------- L1：本地缓存 ----------
        Object local = localCache.getIfPresent(key);
        if (local instanceof RedisData) {
            RedisData redisData = (RedisData) local;
            Object data = redisData.getData();
            if (data == null) {
                // 本地缓存的空值占位
                return null;
            }
            R r = cast(data, type);
            if (redisData.getExpireTime().isAfter(LocalDateTime.now())) {
                return r;
            }
            // 逻辑过期：返回旧值 + 异步重建
            tryRebuild(key, id, type, dbFallback, time, unit);
            return r;
        }

        // ---------- L2：Redis ----------
        String json = stringRedisTemplate.opsForValue().get(key);
        if (json != null && !json.isEmpty()) {
            RedisData redisData = JSONUtil.toBean(json, RedisData.class);
            JSONObject dataObj = (JSONObject) redisData.getData();
            R r = dataObj.toBean(type);
            redisData.setData(r);
            if (redisData.getExpireTime().isAfter(LocalDateTime.now())) {
                localCache.put(key, redisData);
                return r;
            }
            localCache.put(key, redisData);
            tryRebuild(key, id, type, dbFallback, time, unit);
            return r;
        }
        if (json != null) {
            // 空值占位命中：直接返回 null，不打 DB
            return null;
        }

        // ---------- L3：加互斥锁回源 ----------
        return loadWithMutex(key, id, type, dbFallback, time, unit);
    }

    private <R, ID> R loadWithMutex(String key, ID id, Class<R> type,
                                    Function<ID, R> dbFallback, Long time, TimeUnit unit) {
        String lockKey = RedisConstants.LOCK_EVENT_KEY + id;
        String token = UUID.randomUUID().toString(true);
        boolean locked = false;
        try {
            locked = BooleanUtil.isTrue(
                    stringRedisTemplate.opsForValue().setIfAbsent(lockKey, token,
                            RedisConstants.LOCK_EVENT_TTL, TimeUnit.SECONDS));
            if (!locked) {
                // 没抢到锁：短暂等待后重查一次 Redis，仍无则单线程回源。
                // 不做递归，避免锁竞争激烈时线程栈被打爆。
                Thread.sleep(50);
                String retryJson = stringRedisTemplate.opsForValue().get(key);
                if (retryJson != null && !retryJson.isEmpty()) {
                    RedisData redisData = JSONUtil.toBean(retryJson, RedisData.class);
                    R r = ((JSONObject) redisData.getData()).toBean(type);
                    redisData.setData(r);
                    localCache.put(key, redisData);
                    return r;
                }
                if (retryJson != null) {
                    return null;
                }
                return dbFallback.apply(id);
            }
            // 双重检查：可能已有其他线程写好了
            String json = stringRedisTemplate.opsForValue().get(key);
            if (json != null && !json.isEmpty()) {
                RedisData redisData = JSONUtil.toBean(json, RedisData.class);
                R r = ((JSONObject) redisData.getData()).toBean(type);
                redisData.setData(r);
                localCache.put(key, redisData);
                return r;
            }
            R r = dbFallback.apply(id);
            if (r == null) {
                // 空值占位：TTL 短，且带抖动，避免同一时刻批量失效
                stringRedisTemplate.opsForValue().set(key, "",
                        RedisConstants.CACHE_NULL_TTL + ThreadLocalRandom.current().nextInt(3),
                        TimeUnit.MINUTES);
                return null;
            }
            saveWithLogicalExpire(key, r, time, unit);
            return r;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return dbFallback.apply(id);
        } finally {
            // 只有真正持有锁的线程才释放，且校验 token 防止误删别人的锁
            if (locked) {
                unlockSafely(lockKey, token);
            }
        }
    }

    /**
     * 逻辑过期重建：抢不到锁的线程直接返回旧值，不做任何阻塞。
     */
    private <R, ID> void tryRebuild(String key, ID id, Class<R> type,
                                    Function<ID, R> dbFallback, Long time, TimeUnit unit) {
        String lockKey = RedisConstants.LOCK_EVENT_KEY + id;
        String token = UUID.randomUUID().toString(true);
        Boolean locked = stringRedisTemplate.opsForValue().setIfAbsent(lockKey, token,
                RedisConstants.LOCK_EVENT_TTL, TimeUnit.SECONDS);
        if (!BooleanUtil.isTrue(locked)) {
            return;
        }
        CACHE_REBUILD_EXECUTOR.submit(() -> {
            try {
                R r = dbFallback.apply(id);
                if (r == null) {
                    stringRedisTemplate.opsForValue().set(key, "",
                            RedisConstants.CACHE_NULL_TTL, TimeUnit.MINUTES);
                    localCache.invalidate(key);
                    return;
                }
                saveWithLogicalExpire(key, r, time, unit);
                log.debug("缓存重建完成 key={}", key);
            } catch (Exception e) {
                log.error("缓存重建失败 key={}", key, e);
            } finally {
                unlockSafely(lockKey, token);
            }
        });
    }

    /**
     * 写缓存，逻辑过期时间带 ±20% 抖动。
     * 抖动的作用是让同一批 key 的失效时间错开，避免整点同时重建。
     *
     * <p>注意还要给 Redis 一个**物理 TTL**（比逻辑过期晚 10 分钟）：
     * 逻辑过期只影响读路径（读到过期就异步重建），物理 TTL 才是兜底回收。
     * 只写逻辑过期不给物理 TTL 的话，上游 Canal 挂了、某个 key 再没人读，
     * 这条记录就永远不会被回收。
     */
    public void saveWithLogicalExpire(String key, Object value, Long time, TimeUnit unit) {
        long baseMillis = unit.toMillis(time);
        long jitter = (long) (baseMillis * (ThreadLocalRandom.current().nextDouble() * 0.4 - 0.2));
        long logicalMillis = baseMillis + jitter;
        RedisData redisData = new RedisData();
        redisData.setData(value);
        redisData.setExpireTime(LocalDateTime.now().plusNanos(logicalMillis * 1_000_000L));
        stringRedisTemplate.opsForValue().set(
                key,
                JSONUtil.toJsonStr(redisData),
                logicalMillis + PHYSICAL_TTL_GRACE_MILLIS,
                TimeUnit.MILLISECONDS);
        localCache.put(key, redisData);
    }

    /**
     * 失效：Caffeine 与 Redis 同时删。
     * Canal 收到 binlog 后调用它做多实例广播。
     */
    public void evict(String keyPrefix, Object id) {
        String key = keyPrefix + id;
        localCache.invalidate(key);
        stringRedisTemplate.delete(key);
        log.info("缓存已失效 key={}", key);
    }

    private void unlockSafely(String lockKey, String token) {
        try {
            String current = stringRedisTemplate.opsForValue().get(lockKey);
            if (token.equals(current)) {
                stringRedisTemplate.delete(lockKey);
            }
        } catch (Exception e) {
            log.warn("释放缓存锁失败 key={}", lockKey, e);
        }
    }

    @SuppressWarnings("unchecked")
    private <R> R cast(Object data, Class<R> type) {
        if (type.isInstance(data)) {
            return (R) data;
        }
        return JSONUtil.toBean(JSONUtil.toJsonStr(data), type);
    }
}
