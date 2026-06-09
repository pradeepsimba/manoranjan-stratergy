package org.example.hellofx;

import java.util.concurrent.*;

/**
 * Shared ScheduledExecutorService for all background tasks.
 * All tasks run on background threads — use Platform.runLater() for UI updates.
 */
public final class MainScheduler {

    private static final ScheduledExecutorService EXEC =
        Executors.newScheduledThreadPool(4, r -> {
            Thread t = new Thread(r, "trading-scheduler");
            t.setDaemon(true);
            return t;
        });

    private MainScheduler() {}

    public static ScheduledFuture<?> scheduleAtFixedRate(Runnable task, long initDelay, long period, TimeUnit unit) {
        return EXEC.scheduleAtFixedRate(wrapSafe(task), initDelay, period, unit);
    }

    public static ScheduledFuture<?> schedule(Runnable task, long delay, long period, TimeUnit unit) {
        return EXEC.scheduleWithFixedDelay(wrapSafe(task), delay, period, unit);
    }

    public static ScheduledFuture<?> scheduleOnce(Runnable task, long delay, TimeUnit unit) {
        return EXEC.schedule(wrapSafe(task), delay, unit);
    }

    public static void execute(Runnable task) {
        EXEC.execute(wrapSafe(task));
    }

    public static void shutdown() {
        EXEC.shutdownNow();
    }

    private static Runnable wrapSafe(Runnable r) {
        return () -> {
            try { r.run(); }
            catch (Exception e) { System.err.println("Scheduler error: " + e.getMessage()); }
        };
    }
}
