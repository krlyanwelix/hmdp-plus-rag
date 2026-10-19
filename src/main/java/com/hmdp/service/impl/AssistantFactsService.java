package com.hmdp.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.hmdp.entity.Athlete;
import com.hmdp.entity.Event;
import com.hmdp.entity.EventGroup;
import com.hmdp.entity.Registration;
import com.hmdp.service.IAthleteService;
import com.hmdp.service.IEventGroupService;
import com.hmdp.service.IEventService;
import com.hmdp.service.IRegistrationService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;

/**
 * 把「当前登录用户」的私有数据整理成几行结构化事实，注入到 RAG 的 prompt 里。
 *
 * 为什么不把这些数据放进向量库：
 * 向量库是全局可检索的，一旦把「张三的报名单」写进去，李四的提问也可能把它召回来。
 * 私有数据必须**按会话身份查库、按请求注入**，检索层永远碰不到它。
 *
 * 同时这里做了脱敏：送给第三方 LLM 的内容里不能出现完整身份证号。
 */
@Slf4j
@Service
public class AssistantFactsService {

    private static final int MAX_REGISTRATIONS = 5;
    private static final DateTimeFormatter DATE = DateTimeFormatter.ofPattern("yyyy-MM-dd");

    @Resource
    private IRegistrationService registrationService;

    @Resource
    private IEventService eventService;

    @Resource
    private IEventGroupService eventGroupService;

    @Resource
    private IAthleteService athleteService;

    public List<String> build(Long userId) {
        List<String> facts = new ArrayList<>();

        Athlete athlete = athleteService.queryByUserId(userId);
        if (athlete == null) {
            facts.add("该用户尚未完成实名档案，当前不能报名任何项目。");
        } else if (athlete.getBanned() != null && athlete.getBanned() == 1) {
            facts.add("该用户的实名档案处于禁赛状态，不能报名。");
        } else {
            facts.add("实名档案：姓名 " + maskName(athlete.getRealName())
                    + "，证件号 " + maskIdCard(athlete.getIdCard())
                    + "，已完成实名。");
        }

        List<Registration> registrations = registrationService.list(
                new QueryWrapper<Registration>()
                        .eq("user_id", userId)
                        .eq("active_flag", 1)
                        .orderByDesc("create_time")
                        .last("LIMIT " + MAX_REGISTRATIONS));

        if (registrations == null || registrations.isEmpty()) {
            facts.add("该用户目前没有任何有效的报名单。");
            return facts;
        }

        for (Registration r : registrations) {
            Event event = eventService.getById(r.getEventId());
            EventGroup group = eventGroupService.getById(r.getGroupId());
            StringBuilder line = new StringBuilder("报名单 ");
            line.append(r.getId() == null ? "?" : String.valueOf(r.getId()));
            line.append("：");
            line.append(event == null ? "未知赛事" : event.getName());
            line.append(" · ");
            line.append(group == null ? "未知组别" : group.getName());
            line.append("，状态=").append(statusText(r.getStatus()));
            if (r.getCreateTime() != null) {
                line.append("，报名时间=").append(r.getCreateTime().format(DATE));
            }
            if (r.getPayTime() != null) {
                line.append("，支付时间=").append(r.getPayTime().format(DATE));
            }
            if (r.getCancelTime() != null) {
                line.append("，取消时间=").append(r.getCancelTime().format(DATE));
            }
            facts.add(line.toString());
        }

        // 待支付是最容易被追问的状态，单独点出来，避免模型自己推断
        long unpaid = registrations.stream()
                .filter(r -> r.getStatus() != null && r.getStatus() == 1)
                .count();
        if (unpaid > 0) {
            facts.add("其中有 " + unpaid + " 张报名单处于待支付状态，超时未支付会被自动取消并释放名额。");
        }
        return facts;
    }

    private static String statusText(Integer status) {
        if (status == null) {
            return "未知";
        }
        switch (status) {
            case 1:
                return "待支付";
            case 2:
                return "已支付";
            case 4:
                return "已取消（超时或主动退赛）";
            case 6:
                return "已退款";
            default:
                return "状态码 " + status;
        }
    }

    /** 姓名脱敏：张三 -> 张*；欧阳修 -> 欧** */
    static String maskName(String name) {
        if (name == null || name.isEmpty()) {
            return "未知";
        }
        if (name.length() == 1) {
            return name;
        }
        StringBuilder sb = new StringBuilder(name.substring(0, 1));
        for (int i = 1; i < name.length(); i++) {
            sb.append('*');
        }
        return sb.toString();
    }

    /** 证件号脱敏：只留前 4 位与后 4 位 */
    static String maskIdCard(String idCard) {
        if (idCard == null || idCard.length() <= 8) {
            return "未填写";
        }
        return idCard.substring(0, 4) + "********" + idCard.substring(idCard.length() - 4);
    }
}
