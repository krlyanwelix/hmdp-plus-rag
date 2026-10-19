package com.hmdp.dto;

import lombok.Data;

import java.io.Serializable;

/**
 * 报名单消息体。
 * <p>
 * 只带 5 个 Long 字段，不带 LocalDateTime、不带任何业务对象：
 * 消息体越小，序列化歧义越少，消费端也只需要主键就能回表。
 */
@Data
public class RegistrationMessage implements Serializable {

    private static final long serialVersionUID = 1L;

    private Long id;

    private Long userId;

    private Long athleteId;

    private Long eventId;

    private Long groupId;
}
