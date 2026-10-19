-- =====================================================================
-- 演示数据：1 个账号 + 1 份实名档案 + 2 场赛事 + 4 个组别
-- 时间用相对 NOW() 生成，导入后立刻可以报名，不会因为日期过期而报"已截止"
-- =====================================================================

-- 同 schema.sql：不写这行，容器里 latin1 默认会让中文变成双重编码
SET NAMES utf8mb4;

USE `hmdp_plus`;

INSERT INTO `tb_user` (`id`, `phone`, `nick_name`, `icon`)
VALUES (1, '13800000000', 'runner_demo', '/imgs/icons/default.png');

INSERT INTO `tb_athlete` (`user_id`, `real_name`, `id_card`, `gender`, `birthday`, `best_time_seconds`, `banned`)
VALUES (1, '张三', '440101199501011234', 1, '1995-01-01', 13500, 0);

INSERT INTO `tb_event` (`id`, `name`, `city`, `venue`, `description`, `register_begin`, `register_end`, `race_date`, `status`)
VALUES (1, '2026 野径城市半程马拉松', '广州', '天河体育中心',
        '赛事规程：半程马拉松关门时间 3 小时；参赛者需年满 18 周岁且半年内完成过 10 公里以上赛事；'
            '领物时间为赛前两日 10:00-20:00，凭身份证原件领取；赛道沿途每 2.5 公里设补给站。',
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 30 DAY),
        DATE_ADD(CURDATE(), INTERVAL 45 DAY), 1),
       (2, '2026 野径越野挑战赛', '从化', '流溪河森林公园',
        '赛事规程：越野组需自带不少于 1 升的水具；赛道含累计爬升 1200 米；'
            '关门时间 6 小时；强制装备包括头灯、救生哨、保温毯。',
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 20 DAY),
        DATE_ADD(CURDATE(), INTERVAL 35 DAY), 1);

INSERT INTO `tb_event_group` (`event_id`, `name`, `distance_m`, `price`, `quota`, `initial_quota`, `begin_time`, `end_time`)
VALUES (1, '半程马拉松', 21097, 12000, 3000, 3000,
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 30 DAY)),
       (1, '欢乐跑', 5000, 5000, 2000, 2000,
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 30 DAY)),
       (2, '越野 30 公里', 30000, 26000, 800, 800,
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 20 DAY)),
       (2, '越野 50 公里', 50000, 38000, 300, 300,
        DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 20 DAY));
