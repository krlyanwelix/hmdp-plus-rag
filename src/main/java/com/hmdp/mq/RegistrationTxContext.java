package com.hmdp.mq;

import lombok.Data;

/**
 * 事务消息上下文。
 * <p>
 * 由 {@code register()} 在发半消息前创建，作为 {@code sendMessageInTransaction} 的
 * 第二个参数传给事务监听器；{@code executeLocalTransaction} 把 Lua 结果写回
 * {@link #luaResult}，方法返回后由调用方读取，决定是"报名成功"还是"名额已满"。
 * <p>
 * 同一线程内同步传递，因此不需要原子类型。
 * <p>
 * 注意：这里只用 {@code @Data}（它会为 final 字段生成 {@code @RequiredArgsConstructor}），
 * <b>不要</b>加 {@code @AllArgsConstructor}——那会把 luaResult 也算进构造参数，
 * 变成一个 6 参构造器，和调用处对不上。
 */
@Data
public class RegistrationTxContext {

    private final Long registrationId;

    private final Long groupId;

    private final Long eventId;

    private final Long userId;

    private final Long athleteId;

    /** -1 未知（Lua 未执行或执行结果不可判定） / 0 成功 / 1 余量不足 / 2 重复报名 */
    private volatile long luaResult = -1L;
}
