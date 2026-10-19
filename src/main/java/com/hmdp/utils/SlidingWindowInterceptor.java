package com.hmdp.utils;

import com.hmdp.dto.UserDTO;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.annotation.Resource;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/**
 * 第二层限流：按账号的滑动窗口。
 * <p>
 * 第一层（OpenResty 令牌桶）挡的是"全局总量 + 正常连点"，挡不住单个账号的脚本刷；
 * 这一层按 userId 限速，两层目标不同，所以不做成两级令牌桶。
 */
@Slf4j
@Component
public class SlidingWindowInterceptor implements HandlerInterceptor {

    @Resource
    private SlidingWindowRateLimiter rateLimiter;

    @Value("${registration.rate-limit.sliding-window.enabled:true}")
    private boolean enabled;

    @Value("${registration.rate-limit.sliding-window.window-ms:1000}")
    private long windowMs;

    @Value("${registration.rate-limit.sliding-window.max-requests:5}")
    private int maxRequests;

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler)
            throws Exception {
        if (!enabled) {
            return true;
        }
        UserDTO user = UserHolder.getUser();
        if (user == null) {
            // 登录态由 LoginInterceptor 负责，这里不重复判断
            return true;
        }
        String key = RateLimitConstants.SLIDING_WINDOW_REGISTER_KEY + user.getId();
        if (rateLimiter.tryAcquire(key, windowMs, maxRequests)) {
            return true;
        }
        log.warn("滑动窗口限流命中 userId={} uri={}", user.getId(), request.getRequestURI());
        response.setStatus(429);
        response.setContentType("application/json;charset=UTF-8");
        response.getWriter().write("{\"success\":false,\"errorMsg\":\"操作过于频繁，请稍后再试\"}");
        return false;
    }
}
