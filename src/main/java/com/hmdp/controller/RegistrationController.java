package com.hmdp.controller;

import com.hmdp.dto.Result;
import com.hmdp.service.IRegistrationService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.annotation.Resource;

/**
 * 写路径入口。
 * <p>
 * {@code POST /registration/{groupId}} 返回报名号即代表"名额已预占、消息已提交"，
 * 不代表报名单已落库；前端应轮询 {@code /registration/result/{id}} 直到终态。
 */
@RestController
@RequestMapping("/registration")
public class RegistrationController {

    @Resource
    private IRegistrationService registrationService;

    /** 报名（抢名额） */
    @PostMapping("/{groupId}")
    public Result register(@PathVariable("groupId") Long groupId) {
        return registrationService.register(groupId);
    }

    /** 结果查询：PROCESSING 或真实状态 */
    @GetMapping("/result/{id}")
    public Result result(@PathVariable("id") Long id) {
        return registrationService.queryResult(id);
    }

    /** 支付 */
    @PutMapping("/pay/{id}")
    public Result pay(@PathVariable("id") Long id) {
        return registrationService.pay(id);
    }

    /** 退赛 / 取消 */
    @PutMapping("/cancel/{id}")
    public Result cancel(@PathVariable("id") Long id) {
        return registrationService.cancel(id);
    }

    /** 我的报名 */
    @GetMapping("/my")
    public Result mine() {
        return registrationService.mine();
    }
}
