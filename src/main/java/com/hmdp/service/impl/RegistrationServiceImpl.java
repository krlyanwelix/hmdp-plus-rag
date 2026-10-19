package com.hmdp.service.impl;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import cn.hutool.json.JSONUtil;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.dto.Result;
import com.hmdp.dto.UserDTO;
import com.hmdp.entity.Athlete;
import com.hmdp.entity.Event;
import com.hmdp.entity.EventGroup;
import com.hmdp.entity.Registration;
import com.hmdp.mapper.RegistrationMapper;
import com.hmdp.mq.RegistrationTxContext;
import com.hmdp.mq.RocketMQProducer;
import com.hmdp.service.IAthleteService;
import com.hmdp.service.IEventGroupService;
import com.hmdp.service.IEventService;
import com.hmdp.service.IRegistrationService;
import com.hmdp.utils.RedisConstants;
import com.hmdp.utils.RedisIdWorker;
import com.hmdp.utils.UserHolder;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.client.producer.SendResult;
import org.apache.rocketmq.client.producer.SendStatus;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Lazy;
import org.springframework.core.io.ClassPathResource;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.annotation.Resource;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

/**
 * 报名核心链路。
 * <p>
 * 写路径分三段，各自的失败处理完全不同：
 * <ol>
 *   <li><b>预占</b>（同步、入口内完成）：发半消息 → 执行 reserve.lua → COMMIT/ROLLBACK。
 *       这一段决定用户能不能拿到报名号。</li>
 *   <li><b>落库</b>（异步，CREATE 消息）：DB 扣余量 + 插单，同一事务；撞唯一索引则回补 Redis。</li>
 *   <li><b>关单</b>（异步，TIMEOUT 延迟消息）：CAS 改状态 + DB 回补 + Redis 回补。</li>
 * </ol>
 * 三段之间不共享事务，靠"Redis 预占 + 唯一索引 + 状态机 CAS + 定时对账"收敛。
 */
@Slf4j
@Service
public class RegistrationServiceImpl extends ServiceImpl<RegistrationMapper, Registration> implements IRegistrationService {

    private static final DefaultRedisScript<Long> RESERVE_SCRIPT;

    static {
        RESERVE_SCRIPT = new DefaultRedisScript<>();
        RESERVE_SCRIPT.setLocation(new ClassPathResource("lua/reserve.lua"));
        RESERVE_SCRIPT.setResultType(Long.class);
    }

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource
    private RedissonClient redissonClient;

    @Resource
    private RedisIdWorker redisIdWorker;

    @Resource
    private RocketMQProducer rocketMQProducer;

    @Resource
    private IAthleteService athleteService;

    @Resource
    private IEventGroupService groupService;

    @Resource
    private IEventService eventService;

    /**
     * 自注入代理：{@code persist} / {@code closeTimeoutTx} 需要通过代理调用，
     * 否则同类内部调用不经过事务拦截器，@Transactional 形同虚设。
     */
    @Autowired
    @Lazy
    private IRegistrationService self;

    @Value("${registration.timeout-minutes:15}")
    private int timeoutMinutes;

    // ==================================================================
    // 1. 入口：名额预占
    // ==================================================================

    @Override
    public Result register(Long groupId) {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("请先登录");
        }
        Athlete athlete = athleteService.queryByUserId(user.getId());
        if (athlete == null) {
            return Result.fail("请先完善参赛人实名信息");
        }
        if (Integer.valueOf(1).equals(athlete.getBanned())) {
            return Result.fail("该账号存在违规记录，暂不可报名");
        }

        EventGroup group = groupService.getById(groupId);
        if (group == null) {
            return Result.fail("组别不存在");
        }
        LocalDateTime now = LocalDateTime.now();
        if (group.getBeginTime() != null && now.isBefore(group.getBeginTime())) {
            return Result.fail("报名尚未开始");
        }
        if (group.getEndTime() != null && now.isAfter(group.getEndTime())) {
            return Result.fail("报名已截止");
        }

        long registrationId = redisIdWorker.nextId("reg");
        // 受理中标记：解决 UNKNOW 分支"占了名额但用户没拿到单号"的问题。
        // 存完整上下文（不只是 id），对账补单时才能重建 CREATE 消息。
        String pendingKey = pendingKey(user.getId(), group.getEventId());
        RegistrationMessage pending = new RegistrationMessage();
        pending.setId(registrationId);
        pending.setUserId(user.getId());
        pending.setAthleteId(athlete.getId());
        pending.setEventId(group.getEventId());
        pending.setGroupId(groupId);
        stringRedisTemplate.opsForValue().set(pendingKey, JSONUtil.toJsonStr(pending),
                RedisConstants.REG_PENDING_TTL_MINUTES, TimeUnit.MINUTES);

        RegistrationTxContext ctx = new RegistrationTxContext(
                registrationId, groupId, group.getEventId(), user.getId(), athlete.getId());

        SendResult sendResult;
        try {
            sendResult = rocketMQProducer.sendRegisterInTransaction(ctx);
        } catch (Exception e) {
            // 半消息都没发出去，Lua 未执行，零副作用
            log.error("发送半消息失败 registrationId={}", registrationId, e);
            stringRedisTemplate.delete(pendingKey);
            return Result.fail("系统繁忙，请稍后重试");
        }
        if (sendResult == null || sendResult.getSendStatus() != SendStatus.SEND_OK) {
            // 注意：SendStatus 只代表半消息，与本地事务结果无关
            log.warn("半消息未发送成功 registrationId={} status={}",
                    registrationId, sendResult == null ? "null" : sendResult.getSendStatus());
            stringRedisTemplate.delete(pendingKey);
            return Result.fail("系统繁忙，请稍后重试");
        }

        long luaResult = ctx.getLuaResult();
        if (luaResult == 0) {
            return Result.ok(registrationId);
        }
        if (luaResult == 1) {
            stringRedisTemplate.delete(pendingKey);
            return Result.fail("该组别名额已满");
        }
        if (luaResult == 2) {
            stringRedisTemplate.delete(pendingKey);
            return Result.fail("您在本赛事已有报名，请勿重复提交");
        }
        // -1：本地事务结果未知，可能已经占住名额。不能报失败，否则用户重试会得到
        // "已报名"却永远不知道自己的报名号。
        Map<String, Object> processing = new LinkedHashMap<>();
        processing.put("registrationId", registrationId);
        processing.put("status", "PROCESSING");
        processing.put("tip", "报名已受理，正在确认结果，请稍后查询");
        return Result.ok(processing);
    }

    @Override
    public long reserveLocalTx(Long groupId, Long eventId, Long userId, Long registrationId) {
        Long result = stringRedisTemplate.execute(RESERVE_SCRIPT,
                Arrays.asList(
                        RedisConstants.QUOTA_KEY + groupId,
                        RedisConstants.REGISTERED_KEY + eventId,
                        RedisConstants.REG_TXN_KEY + registrationId),
                String.valueOf(userId),
                String.valueOf(RedisConstants.REG_TXN_TTL_SECONDS));
        return result == null ? -1L : result;
    }

    @Override
    public boolean hasTxnMarker(Long registrationId) {
        return Boolean.TRUE.equals(stringRedisTemplate.hasKey(RedisConstants.REG_TXN_KEY + registrationId));
    }

    // ==================================================================
    // 2. 落库：CREATE 消息消费
    // ==================================================================

    @Override
    public void createFromMQ(RegistrationMessage message) {
        if (message == null || message.getId() == null) {
            log.warn("CREATE 消息体为空，丢弃");
            return;
        }
        // 幂等：MQ 至少投递一次，先查单再动手
        if (getById(message.getId()) != null) {
            return;
        }
        // 锁粒度 = 赛事 + 参赛人：同一张证件并发提交时才需要串行
        RLock lock = redissonClient.getLock(
                "lock:reg:" + message.getEventId() + ":" + message.getAthleteId());
        boolean locked = false;
        try {
            locked = lock.tryLock(0, 30, TimeUnit.SECONDS);
            if (!locked) {
                // 抛出让 MQ 重试，而不是像原实现那样静默返回
                throw new IllegalStateException("获取报名锁失败");
            }
            if (getById(message.getId()) != null) {
                return;
            }
            Registration registration = new Registration()
                    .setId(message.getId())
                    .setUserId(message.getUserId())
                    .setAthleteId(message.getAthleteId())
                    .setEventId(message.getEventId())
                    .setGroupId(message.getGroupId())
                    .setStatus(RedisConstants.REG_STATUS_UNPAID)
                    .setActiveFlag(RedisConstants.ACTIVE_FLAG_YES)
                    .setCreateTime(LocalDateTime.now());

            try {
                // 内层事务：DB 扣余量 + 插单，一起成功或一起回滚
                self.persist(registration);
            } catch (DuplicateKeyException e) {
                // 唯一索引 (event_id, athlete_id, active_flag) 兜住了"同一证件重复报名"
                log.warn("撞唯一索引，回补名额 registrationId={}", registration.getId());
                compensateRedis(registration);
                return;
            } catch (IllegalStateException e) {
                log.warn("DB 余量不足，回补名额 registrationId={} reason={}", registration.getId(), e.getMessage());
                compensateRedis(registration);
                return;
            }

            // 落库成功，受理记录可以清掉了：对账只补"消息在路上丢了"的单。
            // 不删的话，补单段每分钟都会把这个已经成功的单再捞出来重投一次。
            stringRedisTemplate.delete(pendingKey(message.getUserId(), message.getEventId()));

            // 事务提交后再发延迟消息。失败由对账任务的"超时关单兜底"覆盖
            try {
                rocketMQProducer.sendTimeout(registration.getId());
            } catch (Exception e) {
                log.error("发送延迟关单消息失败 registrationId={}，等待对账兜底", registration.getId(), e);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("报名落库被中断", e);
        } finally {
            if (locked) {
                lock.unlock();
            }
        }
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void persist(Registration registration) {
        // 条件更新：quota > 0 才扣，返回值必须判断——这是原实现漏掉的一环
        boolean deducted = groupService.update(Wrappers.<EventGroup>lambdaUpdate()
                .setSql("quota = quota - 1")
                .eq(EventGroup::getId, registration.getGroupId())
                .gt(EventGroup::getQuota, 0));
        if (!deducted) {
            throw new IllegalStateException("DB 余量已耗尽");
        }
        save(registration);
    }

    // ==================================================================
    // 3. 状态机：支付 / 取消 / 超时关单
    // ==================================================================

    @Override
    public Result pay(Long registrationId) {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("请先登录");
        }
        Registration registration = getById(registrationId);
        if (registration == null) {
            return Result.fail("报名单不存在");
        }
        if (!user.getId().equals(registration.getUserId())) {
            return Result.fail("无权操作该报名单");
        }
        // CAS：WHERE status = 待支付。支付与超时关单并发时，只有一个能成功
        boolean updated = lambdaUpdate()
                .set(Registration::getStatus, RedisConstants.REG_STATUS_PAID)
                .set(Registration::getPayTime, LocalDateTime.now())
                .eq(Registration::getId, registrationId)
                .eq(Registration::getStatus, RedisConstants.REG_STATUS_UNPAID)
                .update();
        if (!updated) {
            return Result.fail("报名单状态已变更，请刷新后重试");
        }
        return Result.ok();
    }

    @Override
    public Result cancel(Long registrationId) {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("请先登录");
        }
        Registration registration = getById(registrationId);
        if (registration == null) {
            return Result.fail("报名单不存在");
        }
        if (!user.getId().equals(registration.getUserId())) {
            return Result.fail("无权操作该报名单");
        }
        Integer status = registration.getStatus();
        if (status == null
                || status == RedisConstants.REG_STATUS_CANCELLED
                || status == RedisConstants.REG_STATUS_REFUNDED) {
            return Result.fail("报名单已关闭");
        }
        boolean closed = status == RedisConstants.REG_STATUS_UNPAID
                ? self.closeTimeoutTx(registrationId)
                : self.refundTx(registrationId);
        if (!closed) {
            return Result.fail("报名单状态已变更，请刷新后重试");
        }
        releaseRedis(registration.getId(), registration.getGroupId(),
                registration.getEventId(), registration.getUserId());
        return Result.ok();
    }

    @Override
    public void closeTimeout(Long registrationId) {
        boolean closed = self.closeTimeoutTx(registrationId);
        if (!closed) {
            return;
        }
        Registration registration = getById(registrationId);
        if (registration == null) {
            return;
        }
        releaseRedis(registrationId, registration.getGroupId(),
                registration.getEventId(), registration.getUserId());
        log.info("超时关单完成 registrationId={}", registrationId);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public boolean closeTimeoutTx(Long registrationId) {
        Registration registration = getById(registrationId);
        if (registration == null
                || registration.getStatus() == null
                || registration.getStatus() != RedisConstants.REG_STATUS_UNPAID) {
            return false;
        }
        boolean updated = lambdaUpdate()
                .set(Registration::getStatus, RedisConstants.REG_STATUS_CANCELLED)
                .set(Registration::getActiveFlag, RedisConstants.ACTIVE_FLAG_NO)
                .set(Registration::getCancelTime, LocalDateTime.now())
                .eq(Registration::getId, registrationId)
                .eq(Registration::getStatus, RedisConstants.REG_STATUS_UNPAID)
                .update();
        if (!updated) {
            return false;
        }
        // 以订单状态为准回补 DB 名额：CAS 成功才 +1，杜绝重复回流
        groupService.update(Wrappers.<EventGroup>lambdaUpdate()
                .setSql("quota = quota + 1")
                .eq(EventGroup::getId, registration.getGroupId()));
        return true;
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public boolean refundTx(Long registrationId) {
        Registration registration = getById(registrationId);
        if (registration == null
                || registration.getStatus() == null
                || registration.getStatus() != RedisConstants.REG_STATUS_PAID) {
            return false;
        }
        boolean updated = lambdaUpdate()
                .set(Registration::getStatus, RedisConstants.REG_STATUS_REFUNDED)
                .set(Registration::getActiveFlag, RedisConstants.ACTIVE_FLAG_NO)
                .set(Registration::getRefundTime, LocalDateTime.now())
                .eq(Registration::getId, registrationId)
                .eq(Registration::getStatus, RedisConstants.REG_STATUS_PAID)
                .update();
        if (!updated) {
            return false;
        }
        groupService.update(Wrappers.<EventGroup>lambdaUpdate()
                .setSql("quota = quota + 1")
                .eq(EventGroup::getId, registration.getGroupId()));
        return true;
    }

    /** DB 事务已提交后，把 Redis 侧的预占一起释放 */
    private void releaseRedis(Long registrationId, Long groupId, Long eventId, Long userId) {
        try {
            groupService.adjustQuota(groupId, 1);
            stringRedisTemplate.opsForSet()
                    .remove(RedisConstants.REGISTERED_KEY + eventId, String.valueOf(userId));
            stringRedisTemplate.delete(RedisConstants.REG_TXN_KEY + registrationId);
            stringRedisTemplate.delete(pendingKey(userId, eventId));
        } catch (Exception e) {
            log.error("回补 Redis 失败 registrationId={}，等待对账重算", registrationId, e);
        }
    }

    @Override
    public void compensateRedis(Registration registration) {
        try {
            groupService.adjustQuota(registration.getGroupId(), 1);
            stringRedisTemplate.opsForSet()
                    .remove(RedisConstants.REGISTERED_KEY + registration.getEventId(),
                            String.valueOf(registration.getUserId()));
            stringRedisTemplate.delete(RedisConstants.REG_TXN_KEY + registration.getId());
            stringRedisTemplate.delete(pendingKey(registration.getUserId(), registration.getEventId()));
        } catch (Exception e) {
            log.error("回补 Redis 失败 registrationId={}", registration.getId(), e);
        }
    }

    // ==================================================================
    // 4. 查询
    // ==================================================================

    @Override
    public Result queryResult(Long registrationId) {
        Registration registration = getById(registrationId);
        if (registration == null) {
            // 还没落库：有事务标记说明本地事务已成功，只是消费还没跟上
            String marker = stringRedisTemplate.opsForValue()
                    .get(RedisConstants.REG_TXN_KEY + registrationId);
            if (marker != null) {
                Map<String, Object> processing = new LinkedHashMap<>();
                processing.put("registrationId", registrationId);
                processing.put("status", "PROCESSING");
                return Result.ok(processing);
            }
            return Result.fail("报名单不存在");
        }
        fillNames(java.util.Collections.singletonList(registration));
        return Result.ok(registration);
    }

    @Override
    public Result mine() {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("请先登录");
        }
        List<Registration> list = lambdaQuery()
                .eq(Registration::getUserId, user.getId())
                .orderByDesc(Registration::getCreateTime)
                .last("limit 50")
                .list();
        fillNames(list);
        return Result.ok(list);
    }

    /** 批量补齐展示字段，避免逐条查询 */
    private void fillNames(List<Registration> list) {
        if (list == null || list.isEmpty()) {
            return;
        }
        Set<Long> groupIds = list.stream().map(Registration::getGroupId).collect(Collectors.toSet());
        Set<Long> eventIds = list.stream().map(Registration::getEventId).collect(Collectors.toSet());
        Set<Long> athleteIds = list.stream().map(Registration::getAthleteId).collect(Collectors.toSet());

        Map<Long, String> groupNames = new HashMap<>();
        groupService.listByIds(groupIds).forEach(g -> groupNames.put(g.getId(), g.getName()));
        Map<Long, String> eventNames = new HashMap<>();
        eventService.listByIds(eventIds).forEach(e -> eventNames.put(e.getId(), e.getName()));
        Map<Long, Athlete> athletes = new HashMap<>();
        athleteService.listByIds(athleteIds).forEach(a -> athletes.put(a.getId(), a));

        for (Registration r : list) {
            r.setGroupName(groupNames.get(r.getGroupId()));
            r.setEventName(eventNames.get(r.getEventId()));
            Athlete athlete = athletes.get(r.getAthleteId());
            if (athlete != null) {
                r.setAthleteName(athlete.getRealName());
                r.setIdCard(maskIdCard(athlete.getIdCard()));
            }
        }
    }

    private String maskIdCard(String idCard) {
        if (idCard == null || idCard.length() < 8) {
            return idCard;
        }
        return idCard.substring(0, 4) + "**********" + idCard.substring(idCard.length() - 4);
    }

    private String pendingKey(Long userId, Long eventId) {
        return RedisConstants.REG_PENDING_KEY + userId + ":" + eventId;
    }
}
