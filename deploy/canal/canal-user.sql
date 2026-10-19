-- Canal 用的只读 + 复制账号
-- 由 docker-compose 挂到 mysql 容器的 /docker-entrypoint-initdb.d/03-canal-user.sql，
-- 只在数据卷首次初始化时执行一次。
--
-- 宿主机装 Canal 时手工执行同样的语句即可（见 deploy/canal/README.md）。

CREATE USER IF NOT EXISTS 'canal'@'%' IDENTIFIED WITH mysql_native_password BY 'canal123';
GRANT SELECT, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'canal'@'%';
FLUSH PRIVILEGES;
