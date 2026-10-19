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
 * 赛事组别（= 名额池）。
 * <p>
 * 这里对应"秒杀里的那个商品"：{@code quota} 是当前余量，{@code initialQuota} 是不可变的
 * 初始名额，用作对账基准。余量这种高频变化的字段不进多级缓存，只从 Redis 读。
 */
@Data
@EqualsAndHashCode(callSuper = false)
@Accessors(chain = true)
@TableName("tb_event_group")
public class EventGroup implements Serializable {

    private static final long serialVersionUID = 1L;

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    private Long eventId;

    /** 组别名，如 全程马拉松 / 半程马拉松 / 10km 欢乐跑 */
    private String name;

    /** 距离（米） */
    private Integer distanceM;

    /** 报名费（分） */
    private Long price;

    /** 当前余量 */
    private Integer quota;

    /** 初始名额（对账基准，不可变） */
    private Integer initialQuota;

    /** 报名开始 */
    private LocalDateTime beginTime;

    /** 报名截止 */
    private LocalDateTime endTime;

    private LocalDateTime createTime;

    private LocalDateTime updateTime;

    /** 非表字段：Redis 实时余量覆盖后的展示值 */
    @TableField(exist = false)
    private Integer remain;
}
