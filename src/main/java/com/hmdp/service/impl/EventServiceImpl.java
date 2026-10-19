package com.hmdp.service.impl;

import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.Result;
import com.hmdp.entity.Event;
import com.hmdp.mapper.EventMapper;
import com.hmdp.service.IEventGroupService;
import com.hmdp.service.IEventService;
import com.hmdp.utils.MultiLevelCacheService;
import com.hmdp.utils.RedisConstants;
import com.hmdp.utils.SystemConstants;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;

@Slf4j
@Service
public class EventServiceImpl extends ServiceImpl<EventMapper, Event> implements IEventService {

    @Resource
    private MultiLevelCacheService cacheService;

    @Resource
    private IEventGroupService groupService;

    @Override
    public Result queryById(Long id) {
        Event event = cacheService.queryWithMultiLevel(
                RedisConstants.CACHE_EVENT_KEY, id, Event.class,
                this::getById, RedisConstants.CACHE_EVENT_TTL, TimeUnit.MINUTES);
        if (event == null) {
            return Result.fail("赛事不存在");
        }
        return Result.ok(event);
    }

    @Override
    public Result queryDetail(Long eventId) {
        Event event = cacheService.queryWithMultiLevel(
                RedisConstants.CACHE_EVENT_KEY, eventId, Event.class,
                this::getById, RedisConstants.CACHE_EVENT_TTL, TimeUnit.MINUTES);
        if (event == null) {
            return Result.fail("赛事不存在");
        }
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("event", event);
        detail.put("groups", groupService.listByEventId(eventId));
        return Result.ok(detail);
    }

    @Override
    public Result listByPage(int page) {
        if (page < 1) {
            page = 1;
        }
        Page<Event> result = lambdaQuery()
                .eq(Event::getStatus, 1)
                .orderByAsc(Event::getRaceDate)
                .page(new Page<>(page, SystemConstants.DEFAULT_PAGE_SIZE));
        return Result.ok(result.getRecords(), result.getTotal());
    }

    @Override
    public Result create(Event event) {
        if (event.getName() == null || event.getName().trim().isEmpty()) {
            return Result.fail("赛事名称不能为空");
        }
        if (event.getStatus() == null) {
            event.setStatus(1);
        }
        save(event);
        return Result.ok(event.getId());
    }

    @Override
    public Result update(Event event) {
        if (event.getId() == null) {
            return Result.fail("赛事 id 不能为空");
        }
        updateById(event);
        // 写后主动失效；跨实例由 Canal 订阅 binlog 广播兜底
        cacheService.evict(RedisConstants.CACHE_EVENT_KEY, event.getId());
        return Result.ok();
    }
}
