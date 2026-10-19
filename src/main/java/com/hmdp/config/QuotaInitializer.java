package com.hmdp.config;

import com.hmdp.entity.EventGroup;
import com.hmdp.service.IEventGroupService;
import com.hmdp.utils.RedisConstants;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import javax.annotation.Resource;
import java.util.List;

/**
 * 启动时把 MySQL 里的余量播种到 Redis。
 * <p>
 * 用 setIfAbsent 而不是 set：Redis 里已有的值可能比 DB 新（比如服务重启前刚扣过），
 * 直接覆盖会把已经预占掉的名额又"还"回去。
 */
@Slf4j
@Component
public class QuotaInitializer implements ApplicationRunner {

    @Resource
    private IEventGroupService groupService;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Override
    public void run(ApplicationArguments args) {
        List<EventGroup> groups = groupService.list();
        int created = 0;
        for (EventGroup group : groups) {
            Boolean absent = stringRedisTemplate.opsForValue()
                    .setIfAbsent(RedisConstants.QUOTA_KEY + group.getId(),
                            String.valueOf(group.getQuota()));
            if (Boolean.TRUE.equals(absent)) {
                created++;
            }
        }
        log.info("余量播种完成，共 {} 个组别，新写入 {} 个", groups.size(), created);
    }
}
