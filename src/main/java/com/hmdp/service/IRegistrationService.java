package com.hmdp.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.RegistrationMessage;
import com.hmdp.dto.Result;
import com.hmdp.entity.Registration;

public interface IRegistrationService extends IService<Registration> {

    /** 入口：名额预占 + 事务消息 */
    Result register(Long groupId);

    /** 支付成功后的状态流转（CAS） */
    Result pay(Long registrationId);

    /** 主动退赛 */
    Result cancel(Long registrationId);

    /** 结果查询：落库前返回 PROCESSING，落库后返回真实状态 */
    Result queryResult(Long registrationId);

    /** 我的报名单 */
    Result mine();

    /**
     * 事务消息的本地事务：执行 reserve.lua。
     *
     * @return 0 成功 / 1 余量不足 / 2 重复报名
     */
    long reserveLocalTx(Long groupId, Long eventId, Long userId, Long registrationId);

    /** Broker 回查：读事务标记判断本地事务是否已成功 */
    boolean hasTxnMarker(Long registrationId);

    /** 消费 CREATE 消息：落库（含 DB 余量扣减、唯一索引兜底、Redis 回补） */
    void createFromMQ(RegistrationMessage message);

    /** 消费 TIMEOUT 消息 / 对账兜底：超时关单并回补名额 */
    void closeTimeout(Long registrationId);

    /** 内层事务：DB 扣余量 + 插入报名单 */
    void persist(Registration registration);

    /** 内层事务：CAS 关单 + DB 回补余量 */
    boolean closeTimeoutTx(Long registrationId);

    /** 内层事务：已支付单退款 + DB 回补余量 */
    boolean refundTx(Long registrationId);

    /** 回补 Redis 侧的名额占用 */
    void compensateRedis(Registration registration);
}
