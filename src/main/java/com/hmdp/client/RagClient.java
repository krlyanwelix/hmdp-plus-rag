package com.hmdp.client;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hmdp.dto.AssistantAnswerDTO;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestTemplate;

import javax.annotation.PostConstruct;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 赛事助手（RAG）的 HTTP 客户端。
 *
 * 设计要点：
 * 1. **不抛异常**：任何失败都返回 {@link AssistantAnswerDTO#unavailable}，
 *    让助手成为"可降级的旁路"，绝不因为 RAG 服务抖动影响报名主链路。
 * 2. **超时短**：读超时 8s；连接超时 500ms（同机部署，连不上就是没起）。
 * 3. **不重试**：重试只会把延迟翻倍，让用户自己决定要不要再问一次。
 * 4. **身份靠签名传递**：userId 不能放在请求体里由浏览器决定，否则改个数字就能
 *    查别人的报名状态。这里用共享密钥对 (userId, 时间戳, 请求体哈希) 做 HMAC，
 *    Python 侧验签 + 校验 60s 时间窗。
 */
@Slf4j
@Component
public class RagClient {

    @Value("${assistant.rag.base-url:http://127.0.0.1:8090}")
    private String baseUrl;

    @Value("${assistant.rag.enabled:true}")
    private boolean enabled;

    @Value("${assistant.rag.connect-timeout-ms:500}")
    private int connectTimeoutMs;

    @Value("${assistant.rag.read-timeout-ms:8000}")
    private int readTimeoutMs;

    @Value("${assistant.rag.stream-timeout-ms:60000}")
    private int streamTimeoutMs;

    @Value("${assistant.rag.shared-secret:}")
    private String sharedSecret;

    private final ObjectMapper objectMapper;

    private RestTemplate restTemplate;

    public RagClient(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @PostConstruct
    public void init() {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeoutMs);
        // 流式接口要等模型逐字吐，读超时必须比非流式宽得多
        factory.setReadTimeout(Math.max(readTimeoutMs, streamTimeoutMs));
        factory.setBufferRequestBody(true);
        this.restTemplate = new RestTemplate(factory);
        log.info("赛事助手客户端初始化 baseUrl={} enabled={} 签名={} connectTimeout={}ms readTimeout={}ms",
                baseUrl, enabled, sharedSecret != null && !sharedSecret.isEmpty(),
                connectTimeoutMs, Math.max(readTimeoutMs, streamTimeoutMs));
    }

    // ------------------------------------------------------------------ //
    // 非流式
    // ------------------------------------------------------------------ //
    public AssistantAnswerDTO ask(String question, Long eventId) {
        return ask(question, eventId, null, null);
    }

    public AssistantAnswerDTO ask(String question, Long eventId, Long userId, List<String> userFacts) {
        if (!enabled) {
            return AssistantAnswerDTO.unavailable("assistant_disabled");
        }
        long start = System.currentTimeMillis();
        try {
            byte[] body = buildBody(question, eventId, userFacts);
            HttpHeaders headers = headersFor(userId, body);
            ResponseEntity<AssistantAnswerDTO> resp = restTemplate.exchange(
                    baseUrl + "/v1/ask",
                    HttpMethod.POST,
                    new HttpEntity<>(body, headers),
                    AssistantAnswerDTO.class);
            AssistantAnswerDTO answer = resp.getBody();
            if (answer == null) {
                return AssistantAnswerDTO.unavailable("empty_response");
            }
            log.debug("赛事助手调用完成 eventId={} userId={} 耗时={}ms degraded={} refused={}",
                    eventId, userId, System.currentTimeMillis() - start,
                    answer.getDegraded(), answer.getRefused());
            return answer;
        } catch (HttpClientErrorException.TooManyRequests e) {
            // 429 不是「助手挂了」，是「你问太快了」。这两件事对用户的含义完全不同，
            // 不能都折成 degraded=true，否则用户会以为服务故障而反复重试。
            String retryAfter = e.getResponseHeaders() == null
                    ? null : e.getResponseHeaders().getFirst("Retry-After");
            log.debug("赛事助手被限流 userId={} retryAfter={}s", userId, retryAfter);
            return AssistantAnswerDTO.limited(retryAfter);
        } catch (Exception e) {
            log.warn("赛事助手调用失败，降级返回：耗时={}ms err={}",
                    System.currentTimeMillis() - start, e.toString());
            return AssistantAnswerDTO.unavailable("rag_unreachable");
        }
    }

    // ------------------------------------------------------------------ //
    // 流式（SSE）
    // ------------------------------------------------------------------ //
    public interface StreamListener {
        void onMeta(Map<String, Object> data);

        void onDelta(String text);

        void onDone(Map<String, Object> data);

        void onError(String message);
    }

    /**
     * 流式提问：把 Python 侧的 SSE 事件原样转给调用方。
     *
     * 注意：整段读取必须在调用方线程里同步完成，所以 Controller 会把它丢进线程池，
     * 否则会阻塞 Tomcat 的工作线程。
     */
    public void stream(String question,
                       Long eventId,
                       Long userId,
                       List<String> userFacts,
                       StreamListener listener) {
        if (!enabled) {
            listener.onError("助手未启用");
            return;
        }
        try {
            byte[] body = buildBody(question, eventId, userFacts);
            HttpHeaders headers = headersFor(userId, body);
            headers.setAccept(Collections.singletonList(MediaType.TEXT_EVENT_STREAM));

            restTemplate.execute(
                    baseUrl + "/v1/ask/stream",
                    HttpMethod.POST,
                    request -> {
                        request.getHeaders().putAll(headers);
                        request.getBody().write(body);
                    },
                    response -> {
                        // 用 UTF-8 显式解码：默认字符集在 Windows 上可能是 GBK
                        try (BufferedReader reader = new BufferedReader(
                                new InputStreamReader(response.getBody(), StandardCharsets.UTF_8))) {
                            String event = "message";
                            String line;
                            while ((line = reader.readLine()) != null) {
                                if (line.startsWith("event:")) {
                                    event = line.substring(6).trim();
                                } else if (line.startsWith("data:")) {
                                    dispatch(event, line.substring(5).trim(), listener);
                                }
                            }
                        }
                        return null;
                    });
        } catch (HttpClientErrorException.TooManyRequests e) {
            String retryAfter = e.getResponseHeaders() == null
                    ? null : e.getResponseHeaders().getFirst("Retry-After");
            log.debug("赛事助手流式调用被限流 userId={} retryAfter={}s", userId, retryAfter);
            listener.onError(retryAfter == null
                    ? "你问得有点快，请稍后再试"
                    : "你问得有点快，请 " + retryAfter + " 秒后再试");
        } catch (Exception e) {
            // 客户端断开（IOException）也会走到这里，统一按"降级"处理
            log.warn("赛事助手流式调用中断：{}", e.toString());
            listener.onError("赛事助手暂时不可用，请稍后再试");
        }
    }

    @SuppressWarnings("unchecked")
    private void dispatch(String event, String json, StreamListener listener) {
        Map<String, Object> data;
        try {
            data = objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            log.warn("SSE 数据解析失败 event={} json={}", event, json);
            return;
        }
        switch (event) {
            case "meta":
                listener.onMeta(data);
                break;
            case "delta":
                Object text = data.get("text");
                listener.onDelta(text == null ? "" : String.valueOf(text));
                break;
            case "done":
                listener.onDone(data);
                break;
            case "error":
                listener.onError(String.valueOf(data.getOrDefault("message", "生成失败")));
                break;
            default:
                break;
        }
    }

    // ------------------------------------------------------------------ //
    // 工具
    // ------------------------------------------------------------------ //
    private byte[] buildBody(String question, Long eventId, List<String> userFacts) throws Exception {
        Map<String, Object> body = new HashMap<>(8);
        body.put("question", question);
        if (eventId != null) {
            body.put("eventId", eventId);
        }
        if (userFacts != null && !userFacts.isEmpty()) {
            body.put("userFacts", userFacts);
        }
        return objectMapper.writeValueAsBytes(body);
    }

    /**
     * 签名口径必须与 service/security.py 完全一致：
     * HMAC-SHA256(secret, "{user}\n{ts}\n" || sha256(body))
     */
    private HttpHeaders headersFor(Long userId, byte[] body) throws Exception {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        if (userId == null || sharedSecret == null || sharedSecret.isEmpty()) {
            return headers;
        }
        String ts = String.valueOf(System.currentTimeMillis() / 1000);
        headers.set("X-Rag-User", String.valueOf(userId));
        headers.set("X-Rag-Ts", ts);
        headers.set("X-Rag-Sign", sign(String.valueOf(userId), ts, body));
        return headers;
    }

    private String sign(String user, String ts, byte[] body) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(sharedSecret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        mac.update((user + "\n" + ts + "\n").getBytes(StandardCharsets.UTF_8));
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(body);
        return toHex(mac.doFinal(digest));
    }

    private static String toHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }

    /** 探活，供运维接口使用（不抛异常） */
    public boolean healthy() {
        if (!enabled) {
            return false;
        }
        try {
            ResponseEntity<Map> resp = restTemplate.getForEntity(baseUrl + "/healthz", Map.class);
            Map<?, ?> body = resp.getBody();
            return body != null && "ok".equals(String.valueOf(body.get("status")));
        } catch (Exception e) {
            log.warn("赛事助手探活失败：{}", e.toString());
            return false;
        }
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public boolean isEnabled() {
        return enabled;
    }
}
