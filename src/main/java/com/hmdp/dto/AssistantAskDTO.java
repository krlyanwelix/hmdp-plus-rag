package com.hmdp.dto;

import lombok.Data;

/**
 * 赛事助手提问请求。
 *
 * eventId 用于按赛事隔离知识源：RAG 侧会把 eventId 映射成文档前缀（E1- / E2-），
 * 只允许命中该赛事的规程，避免"拿越野赛的规则回答半马的问题"。
 *
 * 校验放在 controller 里做（手写），不引 spring-boot-starter-validation：
 * 这个项目只有一处入参校验，为它多引一个 starter 不划算。
 */
@Data
public class AssistantAskDTO {

    /** 问题正文，controller 里限制 1~500 字 */
    private String question;

    /** 可空：为空时在全部已发布规程中检索 */
    private Long eventId;
}
