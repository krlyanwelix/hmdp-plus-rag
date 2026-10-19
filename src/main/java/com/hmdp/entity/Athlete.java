package com.hmdp.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import lombok.EqualsAndHashCode;
import lombok.experimental.Accessors;

import java.io.Serializable;
import java.time.LocalDate;
import java.time.LocalDateTime;

/**
 * 参赛人档案（实名信息）。
 * <p>
 * 与账号是 1:1：一个账号对应一份实名档案。名额唯一性最终由
 * {@code tb_registration} 上的 (event_id, athlete_id, active_flag) 唯一索引保证，
 * 因为"同一张身份证在同一个赛事里只能有一个有效名额"才是业务真相，而不是"一个账号一次"。
 */
@Data
@EqualsAndHashCode(callSuper = false)
@Accessors(chain = true)
@TableName("tb_athlete")
public class Athlete implements Serializable {

    private static final long serialVersionUID = 1L;

    @TableId(value = "id", type = IdType.AUTO)
    private Long id;

    private Long userId;

    private String realName;

    /** 身份证号：唯一性判定的业务主键 */
    private String idCard;

    /** 0 女 1 男 */
    private Boolean gender;

    private LocalDate birthday;

    /** 历史最好成绩（秒），用于组别准入门槛 */
    private Integer bestTimeSeconds;

    /** 0 正常 1 禁赛（准入校验第一道闸） */
    private Integer banned;

    private LocalDateTime createTime;

    private LocalDateTime updateTime;
}
