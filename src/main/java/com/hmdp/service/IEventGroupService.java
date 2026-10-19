package com.hmdp.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.Result;
import com.hmdp.entity.EventGroup;

import java.util.List;

public interface IEventGroupService extends IService<EventGroup> {

    /** 赛事下的组别列表，返回时用 Redis 实时余量覆盖 quota */
    List<EventGroup> listByEventId(Long eventId);

    /** 创建组别（同时初始化 Redis 余量） */
    Result createGroup(EventGroup group);

    /** 读取实时余量 */
    Integer getRemain(Long groupId);

    /** 相对调整余量（正数回补、负数扣减） */
    void adjustQuota(Long groupId, long delta);

    /** 绝对重设余量（对账用） */
    void resetQuota(Long groupId, int quota);

    /** 失效某赛事下的组别列表缓存（Caffeine + Redis），供 Canal 广播调用 */
    void evictGroupList(Long eventId);
}
