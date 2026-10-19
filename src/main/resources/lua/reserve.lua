-- KEYS[1] = quota:group:{groupId}      组别余量
-- KEYS[2] = reg:event:{eventId}        一人一赛集合（成员为 userId）
-- KEYS[3] = reg:txn:{registrationId}   事务标记，供 Broker 回查
-- ARGV[1] = userId
-- ARGV[2] = 事务标记 TTL（秒）
--
-- 返回值：
--   0  预占成功（余量 -1、写入一人一赛集合、写事务标记）
--   1  余量不足
--   2  该用户在本赛事已有有效报名
--
-- 关键约束：五步必须原子。任何一步失败都不能留下"扣了余量但没有标记"的状态，
-- 否则回查无法判定，名额就永久虚耗了。

local quota = tonumber(redis.call('get', KEYS[1]))
if quota == nil or quota <= 0 then
    return 1
end

if redis.call('sismember', KEYS[2], ARGV[1]) == 1 then
    return 2
end

redis.call('incrby', KEYS[1], -1)
redis.call('sadd', KEYS[2], ARGV[1])
redis.call('set', KEYS[3], '1', 'EX', tonumber(ARGV[2]))

return 0
