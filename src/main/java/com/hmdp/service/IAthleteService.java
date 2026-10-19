package com.hmdp.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.Result;
import com.hmdp.entity.Athlete;

public interface IAthleteService extends IService<Athlete> {

    /** 查询当前账号的参赛人档案 */
    Result queryMine();

    /** 新增 / 修改实名档案（一个账号一份） */
    Result saveProfile(Athlete athlete);

    Athlete queryByUserId(Long userId);
}
