package com.hmdp.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import lombok.EqualsAndHashCode;
import lombok.experimental.Accessors;

import java.io.Serializable;
import java.time.LocalDateTime;

/**
 * 赛事。
 * <p>
 * {@code description} 是面向跑者的赛制说明（路线、关门时间、补给、完赛物品等），
 * 也是后续 RAG 助手最主要的知识源——检索侧直接按 event_id 过滤，不需要跨赛事召回。
 */
@Data
@EqualsAndHashCode(callSuper = false)
@Accessors(chain = true)
@TableName("tb_event")
public class Event implements Serializable {

    private static final long serialVersionUID = 1L;

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    private String name;

    private String city;

    private String venue;

    private String cover;

    /** 赛制说明（长文本，RAG 知识源） */
    private String description;

    /** 报名开放时间 */
    private LocalDateTime registerBegin;

    /** 报名截止时间 */
    private LocalDateTime registerEnd;

    /** 比赛日 */
    private LocalDateTime raceDate;

    /** 0 未开始 1 报名中 2 已截止 3 已完赛 */
    private Integer status;

    private LocalDateTime createTime;

    private LocalDateTime updateTime;

    /** 非表字段：当前赛事下的组别数量，列表页用 */
    @TableField(exist = false)
    private Integer groupCount;
}
