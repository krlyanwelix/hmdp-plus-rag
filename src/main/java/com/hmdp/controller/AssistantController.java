package com.hmdp.controller;

import com.hmdp.client.RagClient;
import com.hmdp.dto.AssistantAnswerDTO;
import com.hmdp.dto.AssistantAskDTO;
import com.hmdp.dto.Result;
import com.hmdp.dto.UserDTO;
import com.hmdp.service.IEventService;
import com.hmdp.service.impl.AssistantFactsService;
import com.hmdp.utils.UserHolder;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import javax.annotation.PreDestroy;
import javax.annotation.Resource;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 赛事助手（RAG）接口。
 *
 * 定位：**旁路**，不在报名写链路上。答的是"赛前咨询"（资格、关门时间、强制装备、
 * 退费规则）以及"我自己的报名状态"。答不出来就明确说没收录，绝不碰库存与订单。
 *
 * 两个接口：
 *   POST /assistant/ask          一次性返回（简单、易缓存）
 *   GET  /assistant/ask/stream   SSE 流式（EventSource 只支持 GET）
 *
 * 用户维度隔离：
 *   1. userId 一律取自 UserHolder（会话），从不让前端传；
 *   2. 用户私有数据由 AssistantFactsService 查库后按请求注入，不进向量库；
 *   3. 调 Python 时带 HMAC 签名，Python 侧验签后才认这个 userId。
 */
@Slf4j
@RestController
@RequestMapping("/assistant")
public class AssistantController {

    private static final int MAX_QUESTION_LENGTH = 500;
    private static final long SSE_TIMEOUT_MS = 60_000L;

    @Resource
    private RagClient ragClient;

    @Resource
    private IEventService eventService;

    @Resource
    private AssistantFactsService factsService;

    /**
     * 流式转发用的线程池：SSE 连接要占住一个线程直到生成结束，
     * 不能直接用 Tomcat 的请求线程（会被慢客户端拖死）。
     */
    private final ExecutorService streamPool = new ThreadPoolExecutor(
            4, 16, 60L, TimeUnit.SECONDS,
            new LinkedBlockingQueue<>(200),
            new java.util.concurrent.ThreadFactory() {
                private final AtomicInteger seq = new AtomicInteger();

                @Override
                public Thread newThread(Runnable r) {
                    Thread t = new Thread(r, "assistant-sse-" + seq.incrementAndGet());
                    t.setDaemon(true);
                    return t;
                }
            },
            new ThreadPoolExecutor.AbortPolicy());

    @PreDestroy
    public void shutdown() {
        streamPool.shutdownNow();
    }

    // ------------------------------------------------------------------ //
    // 非流式
    // ------------------------------------------------------------------ //
    @PostMapping("/ask")
    public Result ask(@RequestBody AssistantAskDTO dto) {
        String error = validate(dto == null ? null : dto.getQuestion(),
                dto == null ? null : dto.getEventId());
        if (error != null) {
            return Result.fail(error);
        }
        String question = dto.getQuestion().trim();
        UserDTO user = UserHolder.getUser();
        List<String> facts = factsService.build(user.getId());

        AssistantAnswerDTO answer = ragClient.ask(question, dto.getEventId(), user.getId(), facts);
        return Result.ok(answer);
    }

    // ------------------------------------------------------------------ //
    // 流式
    // ------------------------------------------------------------------ //
    @GetMapping(value = "/ask/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter askStream(@RequestParam(value = "question", required = false) String question,
                                @RequestParam(value = "eventId", required = false) Long eventId) {
        String error = validate(question, eventId);
        if (error != null) {
            // 参数错也要用 SSE 的形态回，否则前端 EventSource 看到的是 200 + 一段 JSON
            SseEmitter emitter = new SseEmitter(1_000L);
            try {
                emitter.send(SseEmitter.event().name("error").data(errorMap(error)));
                emitter.complete();
            } catch (Exception ignored) {
                emitter.completeWithError(new IllegalArgumentException(error));
            }
            return emitter;
        }

        final String q = question.trim();
        final UserDTO user = UserHolder.getUser();
        final SseEmitter emitter = new SseEmitter(SSE_TIMEOUT_MS);

        emitter.onTimeout(() -> {
            log.warn("助手流式连接超时 userId={}", user.getId());
            emitter.complete();
        });
        emitter.onError(e -> log.warn("助手流式连接异常 userId={} err={}", user.getId(), e.toString()));

        try {
            streamPool.execute(() -> doStream(emitter, q, eventId, user.getId()));
        } catch (Exception e) {
            // 线程池打满：明确告诉前端在排队，而不是静默失败
            log.warn("助手流式线程池拒绝 userId={} err={}", user.getId(), e.toString());
            try {
                emitter.send(SseEmitter.event().name("error").data(errorMap("助手繁忙，请稍后再试")));
            } catch (Exception ignored) {
                // 连接可能已经断了
            }
            emitter.complete();
        }
        return emitter;
    }

    private void doStream(SseEmitter emitter, String question, Long eventId, Long userId) {
        try {
            List<String> facts = factsService.build(userId);
            ragClient.stream(question, eventId, userId, facts, new RagClient.StreamListener() {
                @Override
                public void onMeta(Map<String, Object> data) {
                    send(emitter, "meta", data);
                }

                @Override
                public void onDelta(String text) {
                    Map<String, Object> data = new HashMap<>(2);
                    data.put("text", text);
                    send(emitter, "delta", data);
                }

                @Override
                public void onDone(Map<String, Object> data) {
                    send(emitter, "done", data);
                }

                @Override
                public void onError(String message) {
                    send(emitter, "error", errorMap(message));
                }
            });
            emitter.complete();
        } catch (Exception e) {
            log.warn("助手流式生成失败 userId={} err={}", userId, e.toString());
            send(emitter, "error", errorMap("赛事助手暂时不可用，请稍后再试"));
            emitter.complete();
        }
    }

    private void send(SseEmitter emitter, String event, Map<String, Object> data) {
        try {
            emitter.send(SseEmitter.event().name(event).data(data));
        } catch (Exception e) {
            // 客户端主动断开是最常见的原因，不需要打 error 日志
            log.debug("SSE 推送失败（可能已断开）event={} err={}", event, e.toString());
        }
    }

    private static Map<String, Object> errorMap(String message) {
        Map<String, Object> data = new HashMap<>(2);
        data.put("message", message);
        return data;
    }

    private String validate(String question, Long eventId) {
        if (question == null || question.trim().isEmpty()) {
            return "请输入要咨询的问题";
        }
        if (question.trim().length() > MAX_QUESTION_LENGTH) {
            return "问题过长，请精简到 " + MAX_QUESTION_LENGTH + " 字以内";
        }
        if (eventId != null && eventService.getById(eventId) == null) {
            return "赛事不存在";
        }
        return null;
    }

    /** 助手健康状态：给运维和前端做"是否展示入口"的依据 */
    @GetMapping("/health")
    public Result health() {
        boolean up = ragClient.healthy();
        Map<String, Object> data = new HashMap<>(4);
        data.put("enabled", ragClient.isEnabled());
        data.put("baseUrl", ragClient.getBaseUrl());
        data.put("up", up);
        data.put("degradedMode", !up);
        return Result.ok(data);
    }
}
