package com.hmdp.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.Result;
import com.hmdp.entity.Event;

public interface IEventService extends IService<Event> {

    /** 赛事详情（多级缓存） */
    Result queryById(Long id);

    /** 赛事 + 组别（余量实时） */
    Result queryDetail(Long eventId);

    /** 报名中的赛事分页 */
    Result listByPage(int page);

    /** 创建赛事（运营） */
    Result create(Event event);

    /** 修改赛事并主动失效缓存 */
    Result update(Event event);
}
