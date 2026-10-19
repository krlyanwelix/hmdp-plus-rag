package com.hmdp.dto;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 赛事助手回答。字段与 RAG 服务的 /v1/ask 响应一一对应，
 * 多出来的 degraded / reason 是本项目加的降级语义，前端据此决定是否提示"仅参考"。
 */
@Data
public class AssistantAnswerDTO {

    private String answer;

    private List<Citation> citations = new ArrayList<>();

    /** true 表示回答来自降级路径（检索失败/生成失败），内容仍可用但可能不完整 */
    private Boolean degraded = false;

    /** 降级原因，便于排障：llm_failed / retrieve_failed / assistant_disabled ... */
    private String reason;

    /** 检索层判断"规程里没有"，此时 answer 是固定拒答话术 */
    private Boolean refused = false;

    private Boolean cached = false;

    /** 本次回答用了几段上下文（0 表示检索没命中，走的是拒答话术） */
    private Integer contexts;

    /**
     * true 表示这次回答注入了"当前用户"的私有数据（报名单、实名档案）。
     * 缓存键里带 userId，所以这类回答不会被别的用户命中。
     */
    private Boolean userScoped = false;

    private Integer latencyMs;

    @Data
    public static class Citation {
        private String source;
        private String heading;
        private Double score;
        private String snippet;
    }

    /**
     * 降级结果：RAG 服务不可用时，前端仍然拿到一个可展示的对象，
     * 而不是 500 —— 报名主链路和助手是两条独立路径，助手挂了不能影响报名。
     */
    public static AssistantAnswerDTO unavailable(String reason) {
        AssistantAnswerDTO dto = new AssistantAnswerDTO();
        dto.setAnswer("赛事助手暂时不可用，报名与支付功能不受影响。"
                + "你可以稍后重试，或直接查看《竞赛规程》原文，也可联系组委会 020-8888 6666。");
        dto.setDegraded(true);
        dto.setReason(reason);
        dto.setRefused(false);
        dto.setCached(false);
        dto.setLatencyMs(0);
        return dto;
    }

    /**
     * 被限流：这不是故障，所以 degraded=false，但也不能装成正常回答。
     * retryAfter 来自 Python 侧的 Retry-After 头（秒），拿来给用户一个具体等待时间。
     */
    public static AssistantAnswerDTO limited(String retryAfterSeconds) {
        AssistantAnswerDTO dto = new AssistantAnswerDTO();
        int wait = 0;
        try {
            wait = retryAfterSeconds == null ? 0 : Integer.parseInt(retryAfterSeconds.trim());
        } catch (NumberFormatException ignored) {
            // 头缺失或不是数字就按默认话术回
        }
        dto.setAnswer(wait > 0
                ? "你问得有点快，请 " + wait + " 秒后再试。"
                : "你问得有点快，请稍后再试。");
        dto.setDegraded(false);
        dto.setRefused(true);
        dto.setReason("rate_limited");
        dto.setCached(false);
        dto.setLatencyMs(0);
        return dto;
    }
}
