package com.hmdp.mq;

import cn.hutool.json.JSONUtil;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.service.IRegistrationService;
import com.hmdp.utils.RocketMQConstants;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.client.consumer.DefaultMQPushConsumer;
import org.apache.rocketmq.client.consumer.listener.ConsumeConcurrentlyStatus;
import org.apache.rocketmq.client.consumer.listener.MessageListenerConcurrently;
import org.apache.rocketmq.common.consumer.ConsumeFromWhere;
import org.apache.rocketmq.common.message.MessageExt;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.annotation.PostConstruct;
import javax.annotation.PreDestroy;
import javax.annotation.Resource;
import java.nio.charset.StandardCharsets;

/**
 * 报名消息消费者。
 * <p>
 * 两种 Tag 走两个分支：
 * <ul>
 *   <li>CREATE：落库（扣 DB 余量 + 插单）；</li>
 *   <li>TIMEOUT：到点关单（CAS + 回补）。</li>
 * </ul>
 * 消费失败返回 RECONSUME_LATER，由 RocketMQ 阶梯重试（默认 16 次后进死信队列），
 * 真正的兜底是对账任务，不是无限重试。
 */
@Slf4j
@Component
public class RegistrationMQConsumer {

    @Value("${rocketmq.name-server}")
    private String nameServer;

    @Resource
    private IRegistrationService registrationService;

    private DefaultMQPushConsumer consumer;

    @PostConstruct
    public void start() throws Exception {
        consumer = new DefaultMQPushConsumer(RocketMQConstants.CONSUMER_GROUP);
        consumer.setNamesrvAddr(nameServer);
        consumer.setConsumeFromWhere(ConsumeFromWhere.CONSUME_FROM_FIRST_OFFSET);
        consumer.subscribe(RocketMQConstants.REGISTRATION_TOPIC,
                RocketMQConstants.TAG_CREATE + " || " + RocketMQConstants.TAG_TIMEOUT);
        consumer.registerMessageListener((MessageListenerConcurrently) (messages, context) -> {
            for (MessageExt message : messages) {
                try {
                    String body = new String(message.getBody(), StandardCharsets.UTF_8);
                    RegistrationMessage payload = JSONUtil.toBean(body, RegistrationMessage.class);
                    if (RocketMQConstants.TAG_CREATE.equals(message.getTags())) {
                        registrationService.createFromMQ(payload);
                    } else if (RocketMQConstants.TAG_TIMEOUT.equals(message.getTags())) {
                        registrationService.closeTimeout(payload.getId());
                    } else {
                        log.warn("未知 Tag，跳过 tag={}", message.getTags());
                    }
                } catch (Exception e) {
                    log.error("消费失败，等待重投 msgId={} tag={}", message.getMsgId(), message.getTags(), e);
                    return ConsumeConcurrentlyStatus.RECONSUME_LATER;
                }
            }
            return ConsumeConcurrentlyStatus.CONSUME_SUCCESS;
        });
        consumer.start();
        log.info("报名消费者启动完成 group={}", RocketMQConstants.CONSUMER_GROUP);
    }

    @PreDestroy
    public void shutdown() {
        if (consumer != null) {
            consumer.shutdown();
        }
    }
}
