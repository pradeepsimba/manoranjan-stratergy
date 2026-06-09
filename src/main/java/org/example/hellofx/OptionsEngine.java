package org.example.hellofx;

import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;

/**
 * Black-Scholes ATM option pricing for BankNifty weekly options.
 */
public class OptionsEngine {

    private static volatile java.util.concurrent.ScheduledFuture<?> pollFuture;

    // ── Black-Scholes ─────────────────────────────────────────────────────────

    static double normalCDF(double x) {
        double a1=0.254829592, a2=-0.284496736, a3=1.421413741, a4=-1.453152027, a5=1.061405429, p=0.3275911;
        int sign = x < 0 ? -1 : 1;
        x = Math.abs(x) / Math.sqrt(2);
        double t = 1 / (1 + p * x);
        double y = 1 - (((((a5*t+a4)*t)+a3)*t+a2)*t+a1)*t*Math.exp(-x*x);
        return 0.5 * (1 + sign * y);
    }

    public record BSResult(double price, double delta, double gamma, double theta, double iv) {}

    public static BSResult blackScholes(double S, double K, double T, double r, double sigma, String type) {
        if (T <= 0) {
            double intrinsic = type.equals("CE") ? Math.max(0, S - K) : Math.max(0, K - S);
            return new BSResult(intrinsic, type.equals("CE") ? 1 : 0, 0, 0, sigma);
        }
        double sqrtT = Math.sqrt(T);
        double d1    = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT);
        double d2    = d1 - sigma * sqrtT;
        double Nd1   = normalCDF(d1), Nd2 = normalCDF(d2);
        double Nnd1  = normalCDF(-d1), Nnd2 = normalCDF(-d2);

        double price = type.equals("CE")
            ? S * Nd1 - K * Math.exp(-r * T) * Nd2
            : K * Math.exp(-r * T) * Nnd2 - S * Nnd1;

        double delta = type.equals("CE") ? Nd1 : Nd1 - 1;
        double gamma = Math.exp(-d1*d1/2) / (S * sigma * sqrtT * Math.sqrt(2 * Math.PI));
        double theta = (-(S * Math.exp(-d1*d1/2) * sigma) / (2 * sqrtT * Math.sqrt(2*Math.PI))
                       - r * K * Math.exp(-r * T) * (type.equals("CE") ? Nd2 : Nnd2)) / 365;
        return new BSResult(Math.max(0, price), delta, gamma, -Math.abs(theta), sigma);
    }

    // ── ATM helpers ───────────────────────────────────────────────────────────

    public static int atmStrike(double price) {
        return (int)(Math.round(price / 100.0) * 100);
    }

    /** Next weekly Thursday expiry at 15:30 IST. */
    public static LocalDateTime nextExpiry() {
        LocalDateTime ist = LocalDateTime.now(ZoneId.of("Asia/Kolkata"));
        int day = ist.getDayOfWeek().getValue(); // Mon=1..Sun=7, Thu=4
        int daysUntilThu = (4 - day + 7) % 7;
        if (daysUntilThu == 0 && ist.getHour() * 60 + ist.getMinute() >= 930)
            daysUntilThu = 7;
        return ist.plusDays(daysUntilThu).withHour(15).withMinute(30).withSecond(0).withNano(0);
    }

    /** Time to expiry in years (calendar-days / 365). */
    public static double timeToExpiry() {
        LocalDateTime ist    = LocalDateTime.now(ZoneId.of("Asia/Kolkata"));
        LocalDateTime expiry = nextExpiry();
        long msLeft = ChronoUnit.MILLIS.between(ist, expiry);
        if (msLeft <= 0) return 0;
        return Math.max(0, msLeft / (365.0 * 24 * 3600_000));
    }

    /** Annualised historical vol from BN tick buffer. Floor 20%, cap 70%. */
    public static double estimateVol() {
        Double manualIV = AppState.get().manualIV;
        if (manualIV != null) return manualIV;

        List<Double> prices = AppState.get().bnTickPrices;
        if (prices.size() < 3) {
            List<Candle> bnC = AppState.get().lastNCandles.get(AppConfig.INDEX_SYMBOL);
            if (bnC != null && bnC.size() >= 3) {
                prices = bnC.stream().map(c -> c.close).filter(p -> p > 0).toList();
            }
        }
        if (prices.size() < 3) return 0.28;

        // Take snapshot to avoid CME
        List<Double> snapshot;
        synchronized (prices) { snapshot = new ArrayList<>(prices); }

        double[] returns = new double[snapshot.size() - 1];
        for (int i = 1; i < snapshot.size(); i++) {
            double p0 = snapshot.get(i-1), p1 = snapshot.get(i);
            returns[i-1] = (p0 > 0 && p1 > 0) ? Math.log(p1 / p0) : 0;
        }
        double mean = 0;
        for (double r : returns) mean += r;
        mean /= returns.length;
        double variance = 0;
        for (double r : returns) variance += (r - mean) * (r - mean);
        variance /= Math.max(1, returns.length - 1);
        double std = Math.sqrt(variance);
        // bnTickPrices are ~1-second ticks: 375 min × 60 ticks × 252 days
        double annualVol = std * Math.sqrt(375.0 * 60 * 252);
        return Math.min(Math.max(annualVol, 0.20), 0.70);
    }

    public static BSResult calcATMOption(int strike, String optType, double spot) {
        double T     = timeToExpiry();
        double sigma = estimateVol();
        return blackScholes(spot, strike, T, AppConfig.RISK_FREE_RATE, sigma, optType);
    }

    // ── ATM tracking ──────────────────────────────────────────────────────────

    public static void startATMTracking(String tradeType, double entryPrice) {
        AppState st = AppState.get();
        int    strike  = atmStrike(entryPrice);
        String optType = tradeType.equals("BUY") ? "CE" : "PE";
        BSResult calc  = calcATMOption(strike, optType, entryPrice);

        AtmOption opt      = new AtmOption();
        opt.strike         = strike;
        opt.type           = optType;
        opt.entryPrice     = entryPrice;
        opt.entryPremium   = calc.price();
        opt.currentPremium = calc.price();
        opt.expiryDate     = nextExpiry().toLocalDate().toString();
        opt.iv             = calc.iv();
        opt.delta          = calc.delta();
        opt.theta          = calc.theta();
        st.atmOption       = opt;

        // Poll every 5 seconds
        if (pollFuture != null) pollFuture.cancel(false);
        pollFuture = MainScheduler.schedule(() -> {
            if (st.atmOption == null || st.activeTrade == null) {
                stopATMTracking();
                return;
            }
            List<Candle> bnC = st.lastNCandles.get(AppConfig.INDEX_SYMBOL);
            if (bnC == null || bnC.isEmpty()) return;
            double liveSpot  = bnC.get(bnC.size()-1).close;
            BSResult live    = calcATMOption(st.atmOption.strike, st.atmOption.type, liveSpot);
            st.atmOption.currentPremium = live.price();
            st.atmOption.delta          = live.delta();
            st.atmOption.theta          = live.theta();
            st.atmOption.iv             = live.iv();
        }, 5, 5, java.util.concurrent.TimeUnit.SECONDS);
    }

    public static void stopATMTracking() {
        if (pollFuture != null) { pollFuture.cancel(false); pollFuture = null; }
        AppState.get().atmOption = null;
    }
}
