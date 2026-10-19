package com.hmdp.mq;

import cn.hutool.json.JSONArray;
import cn.hutool.json.JSONObject;
import cn.hutool.json.JSONUtil;
import com.hmdp.service.IEventGroupService;
import com.hmdp.utils.MultiLevelCacheService;
import com.hmdp.utils.RedisConstants;
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
 * Canal binlog 广播 → 缓存失效。
 * <p>
 * 只解决一件事：某个实例（或运维手工改库）更新了赛事数据后，其它实例的 Caffeine
 * 与 Redis 缓存必须跟着失效。Caffeine 在 JVM 内，除了广播没有别的跨实例手段，
 * 所以这条链路在"多级缓存"方案里不是可选项。
 * <p>
 * 消息体约定（Canal 平铺 JSON）：
 * {@code {"table":"tb_event","data":[{"id":123}]}}
 */
@Slf4j
@Component
public class CanalCacheEvictConsumer {

    @Value("${rocketmq.name-server}")
    private String nameServer;

    @Resource
    private MultiLevelCacheService cacheService;

    @Resource
    private IEventGroupService eventGroupService;

    private DefaultMQPushConsumer consumer;

    @PostConstruct
    public void start() throws Exception {
        consumer = new DefaultMQPushConsumer(RocketMQConstants.CANAL_CONSUMER_GROUP);
        consumer.setNamesrvAddr(nameServer);
        consumer.setConsumeFromWhere(ConsumeFromWhere.CONSUME_FROM_LAST_OFFSET);
        consumer.subscribe(RocketMQConstants.CANAL_TOPIC, "*");
        consumer.registerMessageListener((MessageListenerConcurrently) (messages, context) -> {
            for (MessageExt message : messages) {
                try {
                    String body = new String(message.getBody(), StandardCharsets.UTF_8);
                    JSONObject json = JSONUtil.parseObj(body);
                    String table = json.getStr("table");
                    JSONArray data = json.getJSONArray("data");
                    if (data == null) {
                        continue;
                    }
                    for (int i = 0; i < data.size(); i++) {
                        JSONObject row = data.getJSONObject(i);
                        Long id = row.getLong("id");
                        if (id == null) {
                            continue;
                        }
                        if ("tb_event".equals(table)) {
                            cacheService.evict(RedisConstants.CACHE_EVENT_KEY, id);
                        } else if ("tb_event_group".equals(table)) {
                            Long eventId = row.getLong("event_id");
                            if (eventId != null) {
                                eventGroupService.evictGroupList(eventId);
                            }
                        }
                    }
                } catch (Exception e) {
                    log.error("处理 Canal 消息失败 msgId={}", message.getMsgId(), e);
                    return ConsumeConcurrentlyStatus.RECONSUME_LATER;
                }
            }
            return ConsumeConcurrentlyStatus.CONSUME_SUCCESS;
        });
        consumer.start();
        log.info("Canal 缓存失效消费者启动完成");
    }

    @PreDestroy
    public void shutdown() {
        if (consumer != null) {
            consumer.shutdown();
        }
    }
}
