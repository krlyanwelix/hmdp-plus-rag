package com.hmdp.task;

import cn.hutool.core.util.StrUtil;
import cn.hutool.json.JSONUtil;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.entity.EventGroup;
import com.hmdp.entity.Registration;
import com.hmdp.mq.RocketMQProducer;
import com.hmdp.service.IEventGroupService;
import com.hmdp.service.IRegistrationService;
import com.hmdp.utils.RedisConstants;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import javax.annotation.Resource;
import java.time.LocalDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * 名额对账。
 * <p>
 * 主路径（Redis 预占 + MQ 落库）已经覆盖了绝大多数情况，这个任务只做三件兜底：
 * <ol>
 *   <li><b>超时关单兜底</b>：延迟消息丢了、或消费失败进了死信，这里按 create_time 扫出来关掉；</li>
 *   <li><b>漏单补偿</b>：Redis 里已预占、但报名表里没有有效单 —— 说明 CREATE 消息没消费成功。
 *       用 {@code reg:pending:{userId}:{eventId}} 反查报名号并重发普通消息；</li>
 *   <li><b>余量重算</b>：以报名表为账本，{@code expected = 初始名额 − 有效单数}，同时改写
 *       Redis 与 MySQL。这是"账本归位"，不是"再猜一次"。</li>
 * </ol>
 * 只对已过报名截止时间的组别重算，避免和正在进行的报名互相覆盖。
 */
@Slf4j
@Component
public class RegistrationReconcileTask {

    @Resource
    private IRegistrationService registrationService;

    @Resource
    private IEventGroupService groupService;

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource
    private RedissonClient redissonClient;

    @Resource
    private RocketMQProducer rocketMQProducer;

    @Value("${registration.timeout-minutes:15}")
    private int timeoutMinutes;

    @Scheduled(fixedDelay = 60_000)
    public void reconcile() {
        RLock lock = redissonClient.getLock("lock:reg:reconcile");
        boolean locked = false;
        try {
            locked = lock.tryLock();
            if (!locked) {
                log.debug("已有实例在对账，跳过");
                return;
            }
            int closed = closeTimeoutOrders();
            int resent = supplementMissingOrders();
            int fixed = recomputeQuota();
            if (closed + resent + fixed > 0) {
                log.warn("对账完成：关单 {} 条，补单 {} 条，余量修正 {} 处", closed, resent, fixed);
            }
        } catch (Exception e) {
            log.error("对账任务异常", e);
        } finally {
            if (locked) {
                lock.unlock();
            }
        }
    }

    /** ① 超时关单兜底 */
    private int closeTimeoutOrders() {
        List<Registration> list = registrationService.lambdaQuery()
                .eq(Registration::getStatus, RedisConstants.REG_STATUS_UNPAID)
                .lt(Registration::getCreateTime, LocalDateTime.now().minusMinutes(timeoutMinutes))
                .last("limit 200")
                .list();
        int count = 0;
        for (Registration registration : list) {
            try {
                registrationService.closeTimeout(registration.getId());
                count++;
            } catch (Exception e) {
                log.error("对账关单失败 registrationId={}", registration.getId(), e);
            }
        }
        return count;
    }

    /** ② 漏单补偿：Redis 已占位但库内无有效单 */
    private int supplementMissingOrders() {
        List<EventGroup> endedGroups = groupService.lambdaQuery()
                .lt(EventGroup::getEndTime, LocalDateTime.now())
                .last("limit 100")
                .list();
        if (endedGroups.isEmpty()) {
            return 0;
        }
        int resent = 0;
        Set<Long> handledEvents = new HashSet<>();
        for (EventGroup group : endedGroups) {
            Long eventId = group.getEventId();
            if (!handledEvents.add(eventId)) {
                continue;
            }
            Set<String> claimed = stringRedisTemplate.opsForSet()
                    .members(RedisConstants.REGISTERED_KEY + eventId);
            if (claimed == null || claimed.isEmpty()) {
                continue;
            }
            Set<String> created = new HashSet<>();
            registrationService.lambdaQuery()
                    .eq(Registration::getEventId, eventId)
                    .eq(Registration::getActiveFlag, RedisConstants.ACTIVE_FLAG_YES)
                    .in(Registration::getStatus,
                            RedisConstants.REG_STATUS_UNPAID, RedisConstants.REG_STATUS_PAID)
                    .list()
                    .forEach(r -> created.add(String.valueOf(r.getUserId())));

            for (String userId : claimed) {
                if (created.contains(userId)) {
                    continue;
                }
                String pendingJson = stringRedisTemplate.opsForValue()
                        .get(RedisConstants.REG_PENDING_KEY + userId + ":" + eventId);
                if (StrUtil.isBlank(pendingJson)) {
                    log.error("发现无法定位的预占：userId={} eventId={}，需人工核对", userId, eventId);
                    continue;
                }
                try {
                    RegistrationMessage message = JSONUtil.toBean(pendingJson, RegistrationMessage.class);
                    if (message.getId() == null) {
                        continue;
                    }
                    // 已经是有效单则跳过（防止重复补单）
                    if (registrationService.getById(message.getId()) != null) {
                        continue;
                    }
                    rocketMQProducer.resendCreate(message);
                    resent++;
                    log.warn("补单消息已重发 registrationId={}", message.getId());
                } catch (Exception e) {
                    log.error("补单失败 userId={} eventId={}", userId, eventId, e);
                }
            }
        }
        return resent;
    }

    /** ③ 余量重算：expected = 初始名额 − 有效单数 */
    private int recomputeQuota() {
        List<EventGroup> groups = groupService.lambdaQuery()
                .lt(EventGroup::getEndTime, LocalDateTime.now().minusMinutes(2))
                .last("limit 100")
                .list();
        int fixed = 0;
        for (EventGroup group : groups) {
            Integer used = registrationService.lambdaQuery()
                    .eq(Registration::getGroupId, group.getId())
                    .eq(Registration::getActiveFlag, RedisConstants.ACTIVE_FLAG_YES)
                    .in(Registration::getStatus,
                            RedisConstants.REG_STATUS_UNPAID, RedisConstants.REG_STATUS_PAID)
                    .count();
            int expected = group.getInitialQuota() - (used == null ? 0 : used);
            Integer redisRemain = groupService.getRemain(group.getId());
            if (!Objects.equals(redisRemain, expected)) {
                groupService.resetQuota(group.getId(), expected);
                log.warn("余量修正 groupId={} Redis {} -> {}", group.getId(), redisRemain, expected);
                fixed++;
            }
            if (!Objects.equals(group.getQuota(), expected)) {
                int before = group.getQuota() == null ? 0 : group.getQuota();
                groupService.lambdaUpdate()
                        .set(EventGroup::getQuota, expected)
                        .eq(EventGroup::getId, group.getId())
                        .update();
                log.warn("余量修正 groupId={} DB {} -> {}", group.getId(), before, expected);
                fixed++;
            }
        }
        return fixed;
    }
}
