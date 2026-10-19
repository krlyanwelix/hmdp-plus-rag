package com.hmdp.service.impl;

import cn.hutool.core.bean.BeanUtil;
import cn.hutool.json.JSONUtil;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.github.benmanes.caffeine.cache.Cache;
import com.hmdp.dto.Result;
import com.hmdp.entity.EventGroup;
import com.hmdp.mapper.EventGroupMapper;
import com.hmdp.service.IEventGroupService;
import com.hmdp.utils.RedisConstants;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

@Slf4j
@Service
public class EventGroupServiceImpl extends ServiceImpl<EventGroupMapper, EventGroup> implements IEventGroupService {

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource(name = "eventLocalCache")
    private Cache<String, Object> localCache;

    @Override
    public List<EventGroup> listByEventId(Long eventId) {
        String key = RedisConstants.CACHE_EVENT_GROUP_KEY + eventId;
        Object local = localCache.getIfPresent(key);
        if (local instanceof List) {
            return fillRemain(castList(local));
        }
        String json = stringRedisTemplate.opsForValue().get(key);
        if (json != null && !json.isEmpty()) {
            List<EventGroup> list = JSONUtil.toList(json, EventGroup.class);
            localCache.put(key, list);
            return fillRemain(list);
        }
        List<EventGroup> list = lambdaQuery()
                .eq(EventGroup::getEventId, eventId)
                .orderByAsc(EventGroup::getId)
                .list();
        // 组别静态信息（名称/距离/价格/时间窗）变化很低，用短物理 TTL + Canal 失效即可；
        // 真正高频变化的余量不走这条缓存，见 fillRemain
        stringRedisTemplate.opsForValue().set(key, JSONUtil.toJsonStr(list), 60, TimeUnit.SECONDS);
        localCache.put(key, list);
        return fillRemain(list);
    }

    /**
     * 余量是"会被抢的那个数字"，必须准。
     * 所以缓存只存组别静态信息，余量每次从 {@code quota:group:{id}} 实时读并覆盖。
     */
    private List<EventGroup> fillRemain(List<EventGroup> list) {
        if (list == null || list.isEmpty()) {
            return list;
        }
        List<String> keys = new ArrayList<>(list.size());
        for (EventGroup g : list) {
            keys.add(RedisConstants.QUOTA_KEY + g.getId());
        }
        List<String> values = stringRedisTemplate.opsForValue().multiGet(keys);
        List<EventGroup> out = new ArrayList<>(list.size());
        for (int i = 0; i < list.size(); i++) {
            EventGroup src = list.get(i);
            EventGroup vo = BeanUtil.copyProperties(src, EventGroup.class);
            String v = values == null ? null : values.get(i);
            if (v == null) {
                // Redis 里没有余量（首次部署 / 被清理），用 DB 值回填
                int quota = src.getQuota() == null ? 0 : src.getQuota();
                stringRedisTemplate.opsForValue().set(RedisConstants.QUOTA_KEY + src.getId(), String.valueOf(quota));
                vo.setRemain(quota);
            } else {
                vo.setRemain(Integer.valueOf(v));
            }
            out.add(vo);
        }
        return out;
    }

    @Override
    public Result createGroup(EventGroup group) {
        if (group.getInitialQuota() == null || group.getInitialQuota() <= 0) {
            return Result.fail("初始名额必须大于 0");
        }
        group.setQuota(group.getInitialQuota());
        save(group);
        stringRedisTemplate.opsForValue()
                .set(RedisConstants.QUOTA_KEY + group.getId(), String.valueOf(group.getInitialQuota()));
        evictGroupList(group.getEventId());
        return Result.ok(group.getId());
    }

    @Override
    public Integer getRemain(Long groupId) {
        String v = stringRedisTemplate.opsForValue().get(RedisConstants.QUOTA_KEY + groupId);
        if (v == null) {
            EventGroup g = getById(groupId);
            if (g == null) {
                return null;
            }
            stringRedisTemplate.opsForValue()
                    .set(RedisConstants.QUOTA_KEY + groupId, String.valueOf(g.getQuota()));
            return g.getQuota();
        }
        return Integer.valueOf(v);
    }

    @Override
    public void adjustQuota(Long groupId, long delta) {
        stringRedisTemplate.opsForValue().increment(RedisConstants.QUOTA_KEY + groupId, delta);
    }

    @Override
    public void resetQuota(Long groupId, int quota) {
        stringRedisTemplate.opsForValue().set(RedisConstants.QUOTA_KEY + groupId, String.valueOf(quota));
    }

    public void evictGroupList(Long eventId) {
        String key = RedisConstants.CACHE_EVENT_GROUP_KEY + eventId;
        localCache.invalidate(key);
        stringRedisTemplate.delete(key);
    }

    @SuppressWarnings("unchecked")
    private List<EventGroup> castList(Object local) {
        return (List<EventGroup>) local;
    }
}
