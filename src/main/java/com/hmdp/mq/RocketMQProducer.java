package com.hmdp.mq;

import cn.hutool.json.JSONUtil;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.utils.RocketMQConstants;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.client.exception.MQClientException;
import org.apache.rocketmq.client.producer.DefaultMQProducer;
import org.apache.rocketmq.client.producer.SendResult;
import org.apache.rocketmq.client.producer.TransactionMQProducer;
import org.apache.rocketmq.common.message.Message;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.annotation.PostConstruct;
import javax.annotation.PreDestroy;
import javax.annotation.Resource;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * 生产者。
 * <p>
 * 两个 Producer 实例，因为用途不同：
 * <ul>
 *   <li>{@code txProducer} 绑定事务监听器，只发 CREATE 的半消息；</li>
 *   <li>{@code producer} 发 TIMEOUT 延迟消息与对账补单的普通消息。</li>
 * </ul>
 * 混用会出问题：延迟消息不能是半消息，否则 Broker 会一直等 commit。
 */
@Slf4j
@Component
public class RocketMQProducer {

    @Value("${rocketmq.name-server}")
    private String nameServer;

    @Value("${registration.delay-level:14}")
    private int delayLevel;

    @Resource
    private RegistrationTransactionListener transactionListener;

    /**
     * 必须是 {@link TransactionMQProducer}，不能用 {@link DefaultMQProducer}：
     * 事务监听器与 {@code sendMessageInTransaction} 都只定义在前者上。
     */
    private TransactionMQProducer txProducer;

    private DefaultMQProducer producer;

    /** 处理 Broker 事务回查的线程池（不设的话回查回调没有执行器） */
    private ExecutorService transactionExecutor;

    @PostConstruct
    public void start() throws MQClientException {
        transactionExecutor = new ThreadPoolExecutor(2, 5, 60L, TimeUnit.SECONDS,
                new ArrayBlockingQueue<Runnable>(2000),
                r -> {
                    Thread t = new Thread(r, "rocketmq-tx-check");
                    t.setDaemon(true);
                    return t;
                });

        txProducer = new TransactionMQProducer(RocketMQConstants.TX_PRODUCER_GROUP);
        txProducer.setNamesrvAddr(nameServer);
        txProducer.setSendMsgTimeout(3000);
        txProducer.setRetryTimesWhenSendFailed(2);
        txProducer.setExecutorService(transactionExecutor);
        txProducer.setTransactionListener(transactionListener);
        txProducer.start();

        producer = new DefaultMQProducer(RocketMQConstants.PRODUCER_GROUP);
        producer.setNamesrvAddr(nameServer);
        producer.setSendMsgTimeout(3000);
        producer.setRetryTimesWhenSendFailed(2);
        producer.start();

        log.info("RocketMQ 生产者启动完成 nameServer={}, delayLevel={}", nameServer, delayLevel);
    }

    @PreDestroy
    public void shutdown() {
        if (txProducer != null) {
            txProducer.shutdown();
        }
        if (producer != null) {
            producer.shutdown();
        }
        if (transactionExecutor != null) {
            transactionExecutor.shutdown();
        }
    }

    /**
     * 发送事务消息（半消息 → 本地事务 → commit/rollback）。
     * <p>
     * 该方法返回时，commit/rollback 已经<b>发出</b>（oneway，不保证到达 Broker），
     * 也<b>不保证</b>消费已经开始。返回的 SendResult 只描述半消息的发送结果。
     */
    public SendResult sendRegisterInTransaction(RegistrationTxContext ctx) throws Exception {
        RegistrationMessage body = new RegistrationMessage();
        body.setId(ctx.getRegistrationId());
        body.setUserId(ctx.getUserId());
        body.setAthleteId(ctx.getAthleteId());
        body.setEventId(ctx.getEventId());
        body.setGroupId(ctx.getGroupId());

        Message message = new Message(RocketMQConstants.REGISTRATION_TOPIC,
                RocketMQConstants.TAG_CREATE,
                ctx.getRegistrationId().toString(),
                JSONUtil.toJsonStr(body).getBytes(StandardCharsets.UTF_8));
        return txProducer.sendMessageInTransaction(message, ctx);
    }

    /** 投递延迟关单消息 */
    public void sendTimeout(Long registrationId) throws Exception {
        RegistrationMessage body = new RegistrationMessage();
        body.setId(registrationId);
        Message message = new Message(RocketMQConstants.REGISTRATION_TOPIC,
                RocketMQConstants.TAG_TIMEOUT,
                registrationId.toString(),
                JSONUtil.toJsonStr(body).getBytes(StandardCharsets.UTF_8));
        message.setDelayTimeLevel(delayLevel);
        SendResult result = producer.send(message);
        log.debug("延迟关单消息已投递 registrationId={} msgId={} delayLevel={}",
                registrationId, result.getMsgId(), delayLevel);
    }

    /** 对账补单：重发普通 CREATE 消息（名额在 Redis 里已占住，这里只补落库） */
    public void resendCreate(RegistrationMessage body) throws Exception {
        Message message = new Message(RocketMQConstants.REGISTRATION_TOPIC,
                RocketMQConstants.TAG_CREATE,
                body.getId().toString(),
                JSONUtil.toJsonStr(body).getBytes(StandardCharsets.UTF_8));
        producer.send(message);
    }
}
