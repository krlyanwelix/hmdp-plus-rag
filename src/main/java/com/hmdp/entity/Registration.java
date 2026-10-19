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
 * 报名单。
 * <p>
 * {@code activeFlag} 是"有效单"标记：取消/退款时置 NULL，配合唯一索引
 * (event_id, athlete_id, active_flag) 实现"同一赛事内有效单唯一、历史单可留痕"，
 * 避免用 (event_id, athlete_id) 单列唯一索引导致用户退赛后永远无法重报。
 * 失效值用 NULL 而非 0，是因为 MySQL 唯一索引允许多个 NULL 共存。
 */
@Data
@EqualsAndHashCode(callSuper = false)
@Accessors(chain = true)
@TableName("tb_registration")
public class Registration implements Serializable {

    private static final long serialVersionUID = 1L;

    /** 报名号：由 RedisIdWorker 生成（时间戳 << 32 | 序列号），非自增 */
    @TableId(value = "id", type = IdType.INPUT)
    private Long id;

    private Long userId;

    private Long athleteId;

    private Long eventId;

    private Long groupId;

    /** 1 余额 2 支付宝 3 微信（演示用） */
    private Integer payType;

    /** 1 待支付 2 已支付 4 已取消 6 已退款 */
    private Integer status;

    /** 1 有效 0 已失效（取消/退款），唯一索引第三列 */
    private Integer activeFlag;

    private LocalDateTime createTime;

    private LocalDateTime payTime;

    private LocalDateTime cancelTime;

    private LocalDateTime refundTime;

    private LocalDateTime updateTime;

    // ==================== 非表字段 ====================

    @TableField(exist = false)
    private String athleteName;

    @TableField(exist = false)
    private String idCard;

    @TableField(exist = false)
    private String eventName;

    @TableField(exist = false)
    private String groupName;
}
