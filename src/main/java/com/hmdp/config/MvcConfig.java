package com.hmdp.config;

import com.hmdp.utils.LoginInterceptor;
import com.hmdp.utils.RefreshTokenInterceptor;
import com.hmdp.utils.SlidingWindowInterceptor;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import javax.annotation.Resource;

/**
 * 拦截器顺序：
 * <pre>
 * order 0  RefreshTokenInterceptor  补登录态 + 滑动续期（无论是否登录都执行）
 * order 1  LoginInterceptor         需要登录的接口做 401 拦截
 * order 2  SlidingWindowInterceptor 只对报名写入口做按账号限流
 * </pre>
 * 前两个拦截器是手工 new 的（不用 @Component），因为 RefreshTokenInterceptor 需要构造注入
 * StringRedisTemplate；如果在配置类里声明成 @Bean 再注入回同一个配置类，会形成循环依赖。
 */
@Configuration
public class MvcConfig implements WebMvcConfigurer {

    @Resource
    private StringRedisTemplate stringRedisTemplate;

    @Resource
    private SlidingWindowInterceptor slidingWindowInterceptor;

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(new RefreshTokenInterceptor(stringRedisTemplate))
                .addPathPatterns("/**")
                .order(0);

        // 看赛事不需要登录，报名必须要
        registry.addInterceptor(new LoginInterceptor())
                .addPathPatterns("/**")
                .excludePathPatterns(
                        "/user/code",
                        "/user/login",
                        "/event/**",
                        "/error")
                .order(1);

        // 只拦最热的写入口，避免把读请求也计入配额
        registry.addInterceptor(slidingWindowInterceptor)
                .addPathPatterns("/registration/**")
                .order(2);
    }
}
