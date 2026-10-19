package com.hmdp.service.impl;

import cn.hutool.core.util.StrUtil;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.Result;
import com.hmdp.dto.UserDTO;
import com.hmdp.entity.Athlete;
import com.hmdp.mapper.AthleteMapper;
import com.hmdp.service.IAthleteService;
import com.hmdp.utils.UserHolder;
import org.springframework.stereotype.Service;

@Service
public class AthleteServiceImpl extends ServiceImpl<AthleteMapper, Athlete> implements IAthleteService {

    @Override
    public Result queryMine() {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("未登录");
        }
        return Result.ok(queryByUserId(user.getId()));
    }

    @Override
    public Result saveProfile(Athlete athlete) {
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            return Result.fail("未登录");
        }
        if (StrUtil.isBlank(athlete.getRealName())) {
            return Result.fail("请填写真实姓名");
        }
        if (StrUtil.isBlank(athlete.getIdCard()) || athlete.getIdCard().length() < 15) {
            return Result.fail("请填写有效的证件号码");
        }
        Athlete exist = queryByUserId(user.getId());
        if (exist == null) {
            athlete.setUserId(user.getId());
            athlete.setBanned(0);
            save(athlete);
        } else {
            // 允许修改，但不允许把 idCard 改成别人已占用的号码：
            // 唯一性最终由 tb_registration 上的唯一索引裁决
            athlete.setId(exist.getId());
            athlete.setUserId(user.getId());
            updateById(athlete);
        }
        return Result.ok(queryByUserId(user.getId()));
    }

    @Override
    public Athlete queryByUserId(Long userId) {
        return lambdaQuery().eq(Athlete::getUserId, userId).one();
    }
}
