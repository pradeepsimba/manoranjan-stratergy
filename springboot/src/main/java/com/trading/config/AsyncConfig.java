package com.trading.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.Executor;

@Configuration
public class AsyncConfig {

    // Handles @Async indicator calculations triggered by the trade engine
    @Bean(name = "indicatorExecutor")
    public Executor indicatorExecutor() {
        ThreadPoolTaskExecutor exec = new ThreadPoolTaskExecutor();
        exec.setCorePoolSize(2);
        exec.setMaxPoolSize(4);
        exec.setQueueCapacity(50);
        exec.setThreadNamePrefix("indicator-");
        exec.initialize();
        return exec;
    }

    // Parallel HTTP fetches for historical data (5 concurrent API calls on startup/refresh)
    @Bean(name = "historyExecutor")
    public Executor historyExecutor() {
        ThreadPoolTaskExecutor exec = new ThreadPoolTaskExecutor();
        exec.setCorePoolSize(5);
        exec.setMaxPoolSize(5);
        exec.setQueueCapacity(10);
        exec.setThreadNamePrefix("hist-");
        exec.initialize();
        return exec;
    }
}
