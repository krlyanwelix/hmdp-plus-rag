package com.hmdp.mq;

import cn.hutool.json.JSONUtil;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.service.IRegistrationService;
import com.hmdp.utils.RocketMQConstants;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.client.producer.LocalTransactionState;
import org.apache.rocketmq.client.producer.TransactionListener;
import org.apache.rocketmq.common.message.Message;
import org.apache.rocketmq.common.message.MessageExt;
import org.springframework.context.annotation.Lazy;
import org.springframework.stereotype.Component;

import javax.annotation.Resource;

/**
 * 事务消息监听器。
 * <p>
 * {@link #executeLocalTransaction} 与 {@link #checkLocalTransaction} 的分工：
 * <ul>
 *   <li>前者只在"半消息发送成功"后执行一次，做真正的名额预占；</li>
 *   <li>后者是补偿路径，Broker 发现半消息长时间未确认时回调，只读事务标记、绝不重跑 Lua。</li>
 * </ul>
 * 用 {@code reg:txn:{registrationId}} 作为"本地事务是否成功"的唯一判据，是因为标记和
 * 扣减写在同一个脚本里——只要标记在，扣减就一定也发生了。
 */
@Slf4j
@Component
public class RegistrationTransactionListener implements TransactionListener {

    /**
     * 这里必须 {@code @Lazy}：链路是
     * RegistrationServiceImpl → RocketMQProducer → TransactionListener → RegistrationServiceImpl，
     * 直接注入会形成构造期循环依赖。加 @Lazy 后注入的是代理，代理在首次调用时才解析。
     */
    @Lazy
    @Resource
    private IRegistrationService registrationService;

    @Override
    public LocalTransactionState executeLocalTransaction(Message msg, Object arg) {
        RegistrationTxContext ctx = (RegistrationTxContext) arg;
        try {
            long result = registrationService.reserveLocalTx(
                    ctx.getGroupId(), ctx.getEventId(), ctx.getUserId(), ctx.getRegistrationId());
            ctx.setLuaResult(result);
            if (result == 0) {
                return LocalTransactionState.COMMIT_MESSAGE;
            }
            // 1 余量不足 / 2 重复报名：业务上明确失败，回滚半消息
            return LocalTransactionState.ROLLBACK_MESSAGE;
        } catch (Exception e) {
            // 无法判定（例如返回超时但脚本其实已执行），交给回查
            log.error("本地事务执行异常，交由 Broker 回查 registrationId={}", ctx.getRegistrationId(), e);
            return LocalTransactionState.UNKNOW;
        }
    }

    @Override
    public LocalTransactionState checkLocalTransaction(MessageExt msg) {
        try {
            RegistrationMessage body = JSONUtil.toBean(
                    new String(msg.getBody(), java.nio.charset.StandardCharsets.UTF_8),
                    RegistrationMessage.class);
            boolean committed = registrationService.hasTxnMarker(body.getId());
            log.info("事务回查 registrationId={} 结果={}", body.getId(), committed);
            return committed ? LocalTransactionState.COMMIT_MESSAGE : LocalTransactionState.ROLLBACK_MESSAGE;
        } catch (Exception e) {
            log.error("事务回查异常 msgId={}", msg.getMsgId(), e);
            return LocalTransactionState.UNKNOW;
        }
    }
}
