package com.hmdp;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 赛事报名平台启动类。
 * <p>
 * {@code @EnableScheduling} 用于对账任务；名额一致性不依赖定时任务，
 * 但"主路径出错后的收敛"依赖它。
 */
@MapperScan("com.hmdp.mapper")
@SpringBootApplication
@EnableScheduling
public class HmdpPlusApplication {

    public static void main(String[] args) {
        SpringApplication.run(HmdpPlusApplication.class, args);
    }
}
