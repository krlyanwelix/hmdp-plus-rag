package com.hmdp.service;

import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.LoginFormDTO;
import com.hmdp.dto.Result;
import com.hmdp.entity.User;

public interface IUserService extends IService<User> {

    /** 发送短信验证码（演示环境直接打日志） */
    Result sendCode(String phone);

    /** 验证码登录，返回 token */
    Result login(LoginFormDTO loginForm);

    /** 退出登录：删除服务端会话 */
    Result logout(String token);

    /** 当前登录用户 */
    Result me();
}
