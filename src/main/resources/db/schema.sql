-- =====================================================================
-- hmdp-plus-rag · 体育赛事报名平台
-- 五张表：账号 / 参赛人档案 / 赛事 / 组别(名额池) / 报名单
-- 字符集统一 utf8mb4，金额以"分"存 BIGINT，时间统一 DATETIME
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `hmdp_plus` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE `hmdp_plus`;

DROP TABLE IF EXISTS `tb_registration`;
DROP TABLE IF EXISTS `tb_event_group`;
DROP TABLE IF EXISTS `tb_event`;
DROP TABLE IF EXISTS `tb_athlete`;
DROP TABLE IF EXISTS `tb_user`;

-- ---------------------------------------------------------------------
-- 账号（验证码登录，一人一个手机号）
-- ---------------------------------------------------------------------
CREATE TABLE `tb_user` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    `phone`       VARCHAR(11)  NOT NULL COMMENT '手机号',
    `nick_name`   VARCHAR(32)  NOT NULL COMMENT '昵称',
    `icon`        VARCHAR(255) DEFAULT '' COMMENT '头像',
    `create_time` DATETIME     DEFAULT CURRENT_TIMESTAMP,
    `update_time` DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_phone` (`phone`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='账号';

-- ---------------------------------------------------------------------
-- 参赛人实名档案（与账号 1:1）
-- 与 tb_user 拆开的原因：实名信息是报名准入的依据，更新频率和对账号的读取场景不同，
-- 而且一个账号未来可能维护多个"随行人员"，届时只需去掉 user_id 唯一约束。
-- ---------------------------------------------------------------------
CREATE TABLE `tb_athlete` (
    `id`                BIGINT      NOT NULL AUTO_INCREMENT,
    `user_id`           BIGINT      NOT NULL COMMENT '所属账号',
    `real_name`         VARCHAR(32) NOT NULL COMMENT '真实姓名',
    `id_card`           VARCHAR(32) NOT NULL COMMENT '证件号码（对外展示需脱敏）',
    `gender`            TINYINT     DEFAULT 0 COMMENT '0未知 1男 2女',
    `birthday`          DATE        DEFAULT NULL,
    `best_time_seconds` INT         DEFAULT NULL COMMENT '最好成绩（秒），用于资格自测',
    `banned`            TINYINT     NOT NULL DEFAULT 0 COMMENT '0正常 1禁赛/违规',
    `create_time`       DATETIME    DEFAULT CURRENT_TIMESTAMP,
    `update_time`       DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_user` (`user_id`),
    KEY `idx_id_card` (`id_card`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='参赛人实名档案';

-- ---------------------------------------------------------------------
-- 赛事
-- description 是 RAG 的唯一知识源：赛事规程、赛道说明、领物须知都在这里，
-- 切片进向量库后用 event_id 做过滤条件。
-- ---------------------------------------------------------------------
CREATE TABLE `tb_event` (
    `id`             BIGINT       NOT NULL AUTO_INCREMENT,
    `name`           VARCHAR(64)  NOT NULL COMMENT '赛事名称',
    `city`           VARCHAR(32)  NOT NULL COMMENT '城市',
    `venue`          VARCHAR(128) DEFAULT '' COMMENT '起点/场地',
    `cover`          VARCHAR(255) DEFAULT '' COMMENT '封面图',
    `description`    TEXT COMMENT '赛事规程/须知，RAG 知识源',
    `register_begin` DATETIME     NOT NULL COMMENT '报名开始',
    `register_end`   DATETIME     NOT NULL COMMENT '报名截止',
    `race_date`      DATE         NOT NULL COMMENT '比赛日',
    `status`         TINYINT      NOT NULL DEFAULT 1 COMMENT '0未开始 1报名中 2已截止 3已完赛',
    `create_time`    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    `update_time`    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_status_race` (`status`, `race_date`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='赛事';

-- ---------------------------------------------------------------------
-- 组别 = 名额池
-- quota      当前余量，会被并发改写
-- initial_quota 初始名额，创建后不可变，对账时作为唯一基准
--   没有它，对账就只能"拿余量推余量"，越推越离谱
-- ---------------------------------------------------------------------
CREATE TABLE `tb_event_group` (
    `id`            BIGINT      NOT NULL AUTO_INCREMENT,
    `event_id`      BIGINT      NOT NULL,
    `name`          VARCHAR(64) NOT NULL COMMENT '组别名，如"全程马拉松"',
    `distance_m`    INT         DEFAULT 0 COMMENT '距离（米）',
    `price`         BIGINT      NOT NULL DEFAULT 0 COMMENT '报名费（分）',
    `quota`         INT         NOT NULL DEFAULT 0 COMMENT '当前余量',
    `initial_quota` INT         NOT NULL DEFAULT 0 COMMENT '初始名额（对账基准，不可变）',
    `begin_time`    DATETIME    NOT NULL COMMENT '该组别报名开始',
    `end_time`      DATETIME    NOT NULL COMMENT '该组别报名截止',
    `create_time`   DATETIME    DEFAULT CURRENT_TIMESTAMP,
    `update_time`   DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_event` (`event_id`),
    KEY `idx_end_time` (`end_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='赛事组别/名额池';

-- ---------------------------------------------------------------------
-- 报名单
-- id 由 RedisIdWorker 生成，IdType.INPUT，不是自增
-- active_flag：1 有效 / NULL 失效（取消、退款、超时关单）
--
-- 唯一索引把 active_flag 一起带上，这是关键：
--   (event_id, athlete_id, active_flag) -> 同一赛事同一证件"同时"只能有一张有效单；
--   而 (event_id, athlete_id) 会让退赛的人永远无法重报。
-- 失效为什么用 NULL 而不是 0：MySQL 唯一索引不把 NULL 当重复值，
--   同一证件因此可以有任意多张历史失效单；若用 0，第二次退赛就会撞
--   uk_event_athlete_active —— 这是实测踩出来的坑，不是理论推演。
-- 撞索引后的异常就是"重复报名"的最终裁判，Redis 里的 Set 只是前置拦截。
-- ---------------------------------------------------------------------
CREATE TABLE `tb_registration` (
    `id`          BIGINT  NOT NULL COMMENT '报名号（RedisIdWorker 生成）',
    `user_id`     BIGINT  NOT NULL COMMENT '下单账号',
    `athlete_id`  BIGINT  NOT NULL COMMENT '参赛人',
    `event_id`    BIGINT  NOT NULL,
    `group_id`    BIGINT  NOT NULL,
    `pay_type`    TINYINT DEFAULT NULL COMMENT '1余额 2支付宝 3微信',
    `status`      TINYINT NOT NULL DEFAULT 1 COMMENT '1待支付 2已支付 4已取消 6已退款',
    `active_flag` TINYINT NULL DEFAULT 1 COMMENT '1有效 NULL失效',
    `create_time` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `pay_time`    DATETIME DEFAULT NULL,
    `cancel_time` DATETIME DEFAULT NULL,
    `refund_time` DATETIME DEFAULT NULL,
    `update_time` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_event_athlete_active` (`event_id`, `athlete_id`, `active_flag`),
    KEY `idx_user` (`user_id`),
    KEY `idx_group_status` (`group_id`, `status`, `active_flag`),
    KEY `idx_status_create` (`status`, `create_time`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='报名单';
