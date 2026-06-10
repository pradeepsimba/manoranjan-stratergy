package com.trading.engine;

import com.trading.model.AppState.SRLevels;
import com.trading.model.Candle;

import java.util.*;

public class SupportResistanceEngine {

    // Port of c.html detectSupportResistance() + clusterLevels()
    public static SRLevels detect(List<Candle> candles) {
        if (candles == null || candles.size() < 5) return new SRLevels(List.of(), List.of());

        List<Double> supports = new ArrayList<>(), resistances = new ArrayList<>();
        for (int i = 2; i < candles.size() - 2; i++) {
            double p2 = candles.get(i - 2).close, p1 = candles.get(i - 1).close;
            double c  = candles.get(i).close;
            double n1 = candles.get(i + 1).close, n2 = candles.get(i + 2).close;
            if (c < p1 && c < p2 && c < n1 && c < n2) supports.add(c);
            if (c > p1 && c > p2 && c > n1 && c > n2) resistances.add(c);
        }

        List<Double> cs = cluster(supports, 0.25);
        List<Double> cr = cluster(resistances, 0.25);

        // Last 3 key supports / resistances (matching c.html slice(-3))
        return new SRLevels(
            cs.subList(Math.max(0, cs.size() - 3), cs.size()),
            cr.subList(Math.max(0, cr.size() - 3), cr.size())
        );
    }

    private static List<Double> cluster(List<Double> levels, double threshold) {
        if (levels.isEmpty()) return Collections.emptyList();
        List<Double> sorted = new ArrayList<>(levels);
        sorted.sort(Comparator.naturalOrder());

        List<Double> clusters = new ArrayList<>();
        List<Double> group = new ArrayList<>();
        group.add(sorted.get(0));

        for (int i = 1; i < sorted.size(); i++) {
            double prev = sorted.get(i - 1);
            if (prev > 0 && Math.abs(sorted.get(i) - prev) / prev < threshold / 100.0) {
                group.add(sorted.get(i));
            } else {
                clusters.add(avg(group));
                group = new ArrayList<>();
                group.add(sorted.get(i));
            }
        }
        clusters.add(avg(group));
        return clusters;
    }

    private static double avg(List<Double> g) {
        return g.stream().mapToDouble(d -> d).average().orElse(0);
    }
}
