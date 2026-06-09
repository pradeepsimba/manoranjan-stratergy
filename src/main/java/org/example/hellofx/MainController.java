package org.example.hellofx;

import javafx.application.Platform;
import javafx.beans.property.SimpleStringProperty;
import javafx.collections.*;
import javafx.fxml.FXML;
import javafx.scene.canvas.*;
import javafx.scene.control.*;
import javafx.scene.layout.*;
import javafx.scene.paint.Color;

import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * Main FXML controller. All UI update methods run on the JavaFX Application Thread
 * via Platform.runLater(). Background work runs on MainScheduler threads.
 */
public class MainController {

    // ── FXML bindings ──────────────────────────────────────────────────────────
    @FXML private Label   labelApiStatus;
    @FXML private Label   labelWsStatus;
    @FXML private Label   labelGlobalSignal;
    @FXML private Label   labelBreakout;
    @FXML private Label   labelPoints;
    @FXML private Label   labelTime;
    @FXML private Label   labelFunds;
    @FXML private Label   labelActiveTrade;

    @FXML private ComboBox<String> comboInterval;
    @FXML private Spinner<Integer> spinnerNumCandles;
    @FXML private Spinner<Integer> spinnerCandleOffset;

    @FXML private TableView<CandleRow> tableStocks;

    @FXML private TableView<BigTradeRow> tableBigTrades;

    @FXML private TableView<TradeRow> tableTrades;
    @FXML private Label labelTradeSummary;
    @FXML private RadioButton radioToday;
    @FXML private RadioButton radioAll;

    @FXML private VBox   panelEntryLoop;
    @FXML private VBox   panelAtmOption;
    @FXML private VBox   panelSRZone;

    @FXML private Canvas clockCanvas;

    @FXML private TextField fieldOrderPrice;
    @FXML private ComboBox<String> comboOrderType;
    @FXML private Button btnBuy;
    @FXML private Button btnSell;
    @FXML private Button btnExit;

    // ── Runtime ───────────────────────────────────────────────────────────────
    private final AppState    state       = AppState.get();
    private TradeEngine       tradeEngine;
    private WebSocketService  wsService;
    private boolean           dashboardAll = false;

    // Dynamic candle columns in tableStocks
    private final List<TableColumn<CandleRow, String>> candleCols = new ArrayList<>();

    // ── Init ──────────────────────────────────────────────────────────────────

    @FXML
    public void initialize() {
        DatabaseService.init();

        // Interval selector
        comboInterval.setItems(FXCollections.observableArrayList("1m","3m","5m","15m"));
        comboInterval.setValue("5m");
        comboInterval.setOnAction(e -> onIntervalChanged());

        // Spinners
        spinnerNumCandles.setValueFactory(new SpinnerValueFactory.IntegerSpinnerValueFactory(1, 10, 3));
        spinnerNumCandles.valueProperty().addListener((o, ov, nv) -> { state.numCandles = nv; refresh(); });

        spinnerCandleOffset.setValueFactory(new SpinnerValueFactory.IntegerSpinnerValueFactory(0, 200, 0));
        spinnerCandleOffset.valueProperty().addListener((o, ov, nv) -> { state.candleOffset = nv; refresh(); });

        // Order type combo
        comboOrderType.setItems(FXCollections.observableArrayList("MARKET","LIMIT"));
        comboOrderType.setValue("MARKET");
        comboOrderType.setOnAction(e -> {
            boolean isMarket = "MARKET".equals(comboOrderType.getValue());
            fieldOrderPrice.setDisable(isMarket);
            fieldOrderPrice.setOpacity(isMarket ? 0.4 : 1.0);
        });
        fieldOrderPrice.setDisable(true);
        fieldOrderPrice.setOpacity(0.4);

        // Stock candle table
        buildStockTable();

        // Big trades table
        buildBigTradesTable();

        // Trades table
        buildTradesTable();

        // Trade engine
        tradeEngine = new TradeEngine(
            () -> Platform.runLater(this::updateTradesPanel),
            () -> Platform.runLater(this::updateTradesPanel)
        );

        // Clock
        startClockTimer();

        // Main refresh loop
        MainScheduler.scheduleAtFixedRate(this::backgroundLoop, 0, 3, TimeUnit.SECONDS);

        // Dashboard update every 1s
        MainScheduler.scheduleAtFixedRate(
            () -> Platform.runLater(this::updateTradesPanel), 1, 1, TimeUnit.SECONDS);

        // Big-trades update every 3s
        MainScheduler.scheduleAtFixedRate(
            () -> Platform.runLater(this::updateBigTradesPanel), 2, 3, TimeUnit.SECONDS);

        // Initial data load
        MainScheduler.scheduleOnce(this::initialLoad, 0, TimeUnit.SECONDS);
    }

    // ── Background loop ───────────────────────────────────────────────────────

    private void backgroundLoop() {
        if (!state.signalLocked) {
            AppState.EntryDiagnostics diag = tradeEngine.checkTradeEntry();
            Platform.runLater(() -> updateEntryLoopPanel(diag));
        }
        tradeEngine.checkExit();
        updateGlobalSignal();
        Platform.runLater(this::updateActiveTrade);
        Platform.runLater(this::updateFunds);
        Platform.runLater(this::updateAtmPanel);
    }

    // ── Initial load ─────────────────────────────────────────────────────────

    private void initialLoad() {
        Platform.runLater(() -> {
            labelApiStatus.setText("Loading...");
            labelWsStatus.setText("Connecting...");
        });

        // Fetch historical candles
        Map<String, List<Candle>> data = ApiService.fetchHistorical(
            state.selectedInterval, state.numCandles, state.candleOffset);

        if (!data.isEmpty()) {
            state.lastNCandles.clear();
            state.lastNCandles.putAll(data);
            Platform.runLater(() -> {
                labelApiStatus.setText("API OK");
                labelApiStatus.setStyle("-fx-text-fill: #4caf50; -fx-font-weight: bold;");
                rebuildCandleTable();
            });
        } else {
            Platform.runLater(() -> {
                labelApiStatus.setText("API Error");
                labelApiStatus.setStyle("-fx-text-fill: #c93535; -fx-font-weight: bold;");
            });
        }

        // Fetch BN indicator candles (long series)
        List<Candle> bnLong = ApiService.fetchBNIndicatorCandles(state.selectedInterval);
        synchronized (state.bnIndicatorCandles) {
            state.bnIndicatorCandles.clear();
            state.bnIndicatorCandles.addAll(bnLong);
        }

        // S/R levels
        MainScheduler.scheduleOnce(() -> {
            BreakoutEngine.SRLevels sr = BreakoutEngine.detectSupportResistance(
                state.bnIndicatorCandles.isEmpty()
                    ? (state.lastNCandles.getOrDefault(AppConfig.INDEX_SYMBOL, Collections.emptyList()))
                    : state.bnIndicatorCandles);
            state.srLevels.put(AppConfig.INDEX_SYMBOL,
                new AppState.SRLevels(sr.supports(), sr.resistances()));
            Platform.runLater(this::updateSRPanel);
        }, 500, TimeUnit.MILLISECONDS);

        // Start WebSocket
        if (state.candleOffset == 0) startWebSocket();
    }

    private void refresh() {
        if (wsService != null) wsService.disconnect();
        MainScheduler.execute(this::initialLoad);
    }

    private void onIntervalChanged() {
        state.selectedInterval = comboInterval.getValue();
        refresh();
    }

    // ── WebSocket ─────────────────────────────────────────────────────────────

    private void startWebSocket() {
        if (wsService != null) wsService.disconnect();
        wsService = new WebSocketService(
            ticks -> ticks.forEach(this::applyTickUpdate),
            status -> Platform.runLater(() -> {
                labelWsStatus.setText(status);
                String color = status.contains("OK") || status.contains("Connected") ? "#4caf50" : "#c93535";
                labelWsStatus.setStyle("-fx-text-fill: " + color + "; -fx-font-weight: bold;");
            })
        );
        wsService.connect(state.selectedInterval);
    }

    private void applyTickUpdate(WebSocketService.TickUpdate tick) {
        if (!tick.interval.equals(state.selectedInterval) || state.candleOffset > 0) return;

        String symbol = tick.stockSymbol;
        state.lastNCandles.computeIfAbsent(symbol, k -> new ArrayList<>());
        List<Candle> candles = state.lastNCandles.get(symbol);

        // Live close from LTP
        double liveClose = tick.ltp > 0 ? tick.ltp : tick.close;

        // Feed BN tick prices
        if (symbol.equals(AppConfig.INDEX_SYMBOL) && liveClose > 0) {
            synchronized (state.bnTickPrices) {
                state.bnTickPrices.add(liveClose);
                if (state.bnTickPrices.size() > 60) state.bnTickPrices.remove(0);
            }
        }

        // Update BN indicator candles
        if (symbol.equals(AppConfig.INDEX_SYMBOL) && liveClose > 0) {
            synchronized (state.bnIndicatorCandles) {
                if (!state.bnIndicatorCandles.isEmpty()) {
                    Candle last = state.bnIndicatorCandles.get(state.bnIndicatorCandles.size()-1);
                    if (tick.startTime.equals(last.startTime)) {
                        last.close = liveClose;
                        if (tick.high > last.high) last.high = tick.high;
                        if (tick.low  > 0 && tick.low < last.low)  last.low  = tick.low;
                    } else if (tick.startTime.compareTo(last.startTime) > 0) {
                        state.bnIndicatorCandles.add(
                            new Candle(tick.startTime, tick.open > 0 ? tick.open : liveClose,
                                liveClose, tick.high > 0 ? tick.high : liveClose,
                                tick.low  > 0 ? tick.low  : liveClose, tick.volume));
                        if (state.bnIndicatorCandles.size() > 200) state.bnIndicatorCandles.remove(0);
                    }
                }
            }
        }

        // Update display candles
        synchronized (candles) {
            Candle newC = new Candle(tick.startTime,
                tick.open > 0 ? tick.open : liveClose,
                liveClose, tick.high, tick.low, tick.volume);
            int idx = -1;
            for (int i = 0; i < candles.size(); i++) {
                if (tick.startTime.equals(candles.get(i).startTime)) { idx = i; break; }
            }
            if (idx >= 0) {
                candles.set(idx, newC);
            } else {
                Candle latest = candles.isEmpty() ? null : candles.get(candles.size() - 1);
                if (latest == null || tick.startTime.compareTo(latest.startTime) > 0) {
                    candles.add(newC);
                    if (candles.size() > state.numCandles) candles.remove(0);
                }
            }
        }

        // Save tick to DB for big-trades panel
        if (AppConfig.LEADER_STOCKS.contains(tick.stockname) && tick.volume > 0) {
            DatabaseService.addStockRecord(tick.stockname, tick.startTime, liveClose, tick.volume);
            state.latestMinuteQty.merge(tick.stockname, tick.volume, Double::sum);
        }

        // Update breakout text
        List<Candle> bnC = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        if (bnC != null) {
            state.breakoutText = BreakoutEngine.detectBreakouts(bnC);
        }

        Platform.runLater(() -> {
            refreshCandleRow(symbol, tick);
            updateColumnCounts();
            labelBreakout.setText(state.breakoutText);
        });
    }

    // ── Stock candle table ────────────────────────────────────────────────────

    private void buildStockTable() {
        TableColumn<CandleRow, String> nameCol = new TableColumn<>("Stock");
        nameCol.setCellValueFactory(cd -> new SimpleStringProperty(cd.getValue().name));
        nameCol.setPrefWidth(160);
        tableStocks.getColumns().add(nameCol);

        TableColumn<CandleRow, String> buyQtyCol = new TableColumn<>("BuyQty");
        buyQtyCol.setCellValueFactory(cd -> new SimpleStringProperty(cd.getValue().buyQty));
        buyQtyCol.setPrefWidth(70);

        TableColumn<CandleRow, String> sellQtyCol = new TableColumn<>("SellQty");
        sellQtyCol.setCellValueFactory(cd -> new SimpleStringProperty(cd.getValue().sellQty));
        sellQtyCol.setPrefWidth(70);

        // Candle columns added dynamically in rebuildCandleTable()
        tableStocks.getColumns().addAll(buyQtyCol, sellQtyCol);
        tableStocks.setItems(FXCollections.observableArrayList());
        styleTable(tableStocks);
    }

    private void rebuildCandleTable() {
        // Remove dynamic candle columns (keep name, buyQty, sellQty)
        tableStocks.getColumns().removeAll(candleCols);
        candleCols.clear();

        int n = state.numCandles;
        for (int i = n - 1; i >= 0; i--) {
            final int idx = i;
            String label = i == n-1 ? "Latest" : i == n-2 ? "Prev" : i == n-3 ? "Prev2" : "P" + i;
            TableColumn<CandleRow, String> col = new TableColumn<>(label);
            col.setCellValueFactory(cd -> new SimpleStringProperty(cd.getValue().candleValues.getOrDefault(idx, "N/A")));
            col.setPrefWidth(65);
            col.setCellFactory(tc -> new TableCell<>() {
                @Override protected void updateItem(String item, boolean empty) {
                    super.updateItem(item, empty);
                    if (empty || item == null) { setText(null); setStyle(""); return; }
                    setText(item);
                    try {
                        double v = Double.parseDouble(item);
                        setStyle(v > 0 ? "-fx-background-color: #1a4a1a; -fx-text-fill: #4caf50;"
                            : v < 0    ? "-fx-background-color: #4a1a1a; -fx-text-fill: #c93535;"
                            :            "-fx-background-color: #222; -fx-text-fill: #888;");
                    } catch (NumberFormatException e) { setStyle("-fx-background-color: #222; -fx-text-fill: #888;"); }
                }
            });
            candleCols.add(col);
        }
        // Insert after name column (index 0)
        tableStocks.getColumns().addAll(1, candleCols);

        // Populate rows
        ObservableList<CandleRow> rows = FXCollections.observableArrayList();
        for (AppConfig.Stock stock : AppConfig.STOCKS) {
            rows.add(buildCandleRow(stock));
        }
        tableStocks.setItems(rows);
    }

    private CandleRow buildCandleRow(AppConfig.Stock stock) {
        CandleRow row = new CandleRow();
        row.name   = stock.name() + " (" + stock.symbol() + ")";
        row.symbol = stock.symbol();
        List<Candle> candles = state.lastNCandles.getOrDefault(stock.symbol(), Collections.emptyList());
        for (int i = 0; i < state.numCandles; i++) {
            if (i < candles.size()) {
                Candle c = candles.get(i);
                double diff = c.close - c.open;
                row.candleValues.put(i, String.format("%.2f", diff));
            }
        }
        row.buyQty  = "N/A";
        row.sellQty = "N/A";
        return row;
    }

    private void refreshCandleRow(String symbol, WebSocketService.TickUpdate tick) {
        ObservableList<CandleRow> rows = tableStocks.getItems();
        for (CandleRow row : rows) {
            if (!row.symbol.equals(symbol)) continue;
            // Snapshot the list to avoid CME — WebSocket thread holds per-list lock while writing
            List<Candle> raw = state.lastNCandles.getOrDefault(symbol, Collections.emptyList());
            List<Candle> snapshot;
            synchronized (raw) { snapshot = new ArrayList<>(raw); }
            row.candleValues.clear();
            for (int i = 0; i < snapshot.size(); i++) {
                Candle c = snapshot.get(i);
                row.candleValues.put(i, String.format("%.2f", c.close - c.open));
            }
            row.buyQty  = tick.buyQty  > 0 ? String.valueOf(tick.buyQty)  : "N/A";
            row.sellQty = tick.sellQty > 0 ? String.valueOf(tick.sellQty) : "N/A";
            break;
        }
        tableStocks.refresh();
    }

    private void updateColumnCounts() {
        // Update global signal label
        updateGlobalSignalUI();
    }

    private void updateGlobalSignalUI() {
        String sig  = state.globalSignal;
        String color = state.globalSignalColor;
        labelGlobalSignal.setText("SIGNAL: " + sig);
        labelGlobalSignal.setStyle("-fx-text-fill: " + color + "; -fx-font-weight: bold;");
    }

    // ── Global signal calculation ─────────────────────────────────────────────

    private void updateGlobalSignal() {
        int n = state.numCandles;
        int totalGreen = 0, totalRed = 0;
        int[] greenPerCol = new int[n], redPerCol = new int[n];

        for (AppConfig.Stock stock : AppConfig.STOCKS) {
            List<Candle> candles = state.lastNCandles.getOrDefault(stock.symbol(), Collections.emptyList());
            for (int i = 0; i < n && i < candles.size(); i++) {
                Candle c = candles.get(i);
                if (c.close > c.open) { greenPerCol[i]++; totalGreen++; }
                else if (c.close < c.open) { redPerCol[i]++; totalRed++; }
            }
        }

        boolean allGreen = true, allRed = true;
        for (int i = 0; i < n; i++) {
            if (greenPerCol[i] < 4) allGreen = false;
            if (redPerCol[i]   < 5) allRed   = false;
        }

        String countSig   = allGreen ? "BUY" : allRed ? "SELL" : "NEUTRAL";
        String countColor = allGreen ? "green" : allRed ? "red" : "#777";

        // Weighted % prediction
        double weightedPct = 0, totalWeight = 0;
        for (Map.Entry<String, Double> e : AppConfig.INDEX_WEIGHTS.entrySet()) {
            double w = e.getValue() / 100.0;
            List<Candle> candles = state.lastNCandles.getOrDefault(e.getKey(), Collections.emptyList());
            if (!candles.isEmpty()) {
                Candle c = candles.get(candles.size() - 1);
                double pct = c.open > 0 ? ((c.close - c.open) / c.open) * 100 : 0;
                weightedPct += w * pct;
                totalWeight += w;
            }
        }
        if (totalWeight > 0) weightedPct /= totalWeight;

        String finalSig   = countSig;
        String finalColor = countColor;
        if (Math.abs(weightedPct) > 0.08) {
            finalSig   = weightedPct > 0 ? "STRONG BUY" : "STRONG SELL";
            finalColor = weightedPct > 0 ? "#00ff00" : "#ff0000";
        }

        double fwp = weightedPct;
        String fsig = finalSig, fcolor = finalColor;
        state.globalSignal      = fsig;
        state.globalSignalColor = fcolor;
        Platform.runLater(this::updateGlobalSignalUI);
    }

    // ── Big trades panel ──────────────────────────────────────────────────────

    private void buildBigTradesTable() {
        tableBigTrades.setPlaceholder(new Label("No big-trade data yet"));
        styleTable(tableBigTrades);
    }

    private void updateBigTradesPanel() {
        // Rebuild columns dynamically for leader stocks
        tableBigTrades.getColumns().clear();
        List<DatabaseService.StockTick> ticks = DatabaseService.getTodayStockTicks();
        if (ticks.isEmpty()) return;

        // Group by stock + bucket
        Map<String, Map<String, double[]>> data = new LinkedHashMap<>();
        int intervalMin = switch (state.selectedInterval) {
            case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1;
        };
        for (DatabaseService.StockTick tick : ticks) {
            if (!AppConfig.LEADER_STOCKS.contains(tick.stockname())) continue;
            String bucket = toBucket(tick.time(), intervalMin);
            data.computeIfAbsent(tick.stockname(), k -> new TreeMap<>())
                .merge(bucket, new double[]{tick.qty(), tick.ltp()},
                    (a, b) -> new double[]{a[0]+b[0], b[1]});
        }

        List<String> stocks = new ArrayList<>(data.keySet());
        if (stocks.isEmpty()) return;

        for (String stock : stocks) {
            String shortName = stock.split(" ")[0];
            TableColumn<BigTradeRow, String> col = new TableColumn<>(shortName);
            String finalStock = stock;
            col.setCellValueFactory(cd -> {
                Map<String, double[]> sd = data.get(finalStock);
                if (sd == null) return new SimpleStringProperty("-");
                // Get row index
                int ri = tableBigTrades.getItems().indexOf(cd.getValue());
                List<String> sorted = new ArrayList<>(sd.keySet());
                Collections.sort(sorted, Collections.reverseOrder());
                if (ri < 0 || ri >= sorted.size()) return new SimpleStringProperty("-");
                double[] vals = sd.get(sorted.get(ri));
                return new SimpleStringProperty(String.format("%s\n(%.0f)", sorted.get(ri), vals[0]));
            });
            col.setPrefWidth(90);
            tableBigTrades.getColumns().add(col);
        }

        // Rows: up to 10
        ObservableList<BigTradeRow> rows = FXCollections.observableArrayList();
        for (int i = 0; i < 10; i++) rows.add(new BigTradeRow(i));
        tableBigTrades.setItems(rows);
    }

    private String toBucket(String time, int intervalMin) {
        if (time == null || time.length() < 16) return time;
        try {
            String timePart = time.substring(11, 16);
            String[] parts = timePart.split(":");
            int h = Integer.parseInt(parts[0]);
            int m = (Integer.parseInt(parts[1]) / intervalMin) * intervalMin;
            return String.format("%02d:%02d", h, m);
        } catch (Exception e) { return time.substring(11, Math.min(16, time.length())); }
    }

    // ── Entry loop panel ─────────────────────────────────────────────────────

    private void updateEntryLoopPanel(AppState.EntryDiagnostics d) {
        if (d == null || panelEntryLoop == null) return;
        panelEntryLoop.getChildren().clear();

        BNIndicators ind = d.bnInd();
        boolean macdMet  = ind != null && ind.macdDir != null && !ind.macdDir.equals("—") && !ind.macdDir.equals("NEUTRAL");
        boolean emaMet   = ind != null && ind.emaStack != null && (ind.emaStack.bullish || ind.emaStack.bearish);
        boolean gateOk   = ind != null && (ind.bullish || ind.bearish);
        boolean coolOk   = d.cooldownMs() >= 60_000;
        boolean sidewOk  = d.sidewaysRange() != null && d.sidewaysRange() >= 12;

        boolean[] checks = { d.marketOpen(), d.timeWindowOk(), d.noActiveTrade(),
            coolOk, sidewOk, d.candleCloseOk(), !d.alreadyTradedCandle(),
            d.leaderSignalType().equals("BUY") || d.leaderSignalType().equals("SELL"),
            Math.max(d.green(), d.red()) >= AppConfig.SAME_DIRECTION_REQUIRED,
            d.strongQty() >= AppConfig.SAME_DIRECTION_REQUIRED,
            macdMet, emaMet, gateOk };

        int met = 0; for (boolean b : checks) if (b) met++;
        boolean allOk = met == checks.length;

        String summaryColor = allOk ? "#39aa39" : met >= checks.length - 2 ? "#e67e00" : "#c93535";
        String summaryLabel = allOk ? "✔ ENTRY READY"
            : String.format("✘ BLOCKED  (%d/%d passed)", met, checks.length);

        Label summary = new Label(summaryLabel);
        summary.setStyle("-fx-text-fill: " + summaryColor + "; -fx-font-weight: bold; -fx-font-size: 13px;");
        panelEntryLoop.getChildren().add(summary);

        // Grid of conditions
        GridPane grid = new GridPane();
        grid.setHgap(6); grid.setVgap(3);
        String[][] rows = {
            {"Market Open",         d.marketOpen() ? "✔" : "✘"},
            {"Time Window",         d.timeWindowOk() ? "✔" : "✘"},
            {"No Active Trade",     d.noActiveTrade() ? "✔" : "✘"},
            {"Cooldown >60s",       coolOk  ? "✔" : "✘"},
            {"Sideways ≥12pts",     sidewOk ? "✔" : "✘"},
            {"Candle Closed",       d.candleCloseOk() ? "✔" : "✘"},
            {"New Candle",          !d.alreadyTradedCandle() ? "✔" : "✘"},
            {"Leader Signal",       d.leaderSignalType() + " " + (gateOk ? "✔" : "")},
            {"Dir Count ≥" + AppConfig.SAME_DIRECTION_REQUIRED,
                "G:" + d.green() + " R:" + d.red()},
            {"Strong Qty ≥" + AppConfig.SAME_DIRECTION_REQUIRED, d.strongQty() + "/6"},
            {"MACD",                ind != null ? ind.macdDir : "—"},
            {"EMA Stack",           emaMet ? (ind.emaStack.bullish ? "▲ Bullish" : "▼ Bearish") : "~"},
            {"BN Gate",             gateOk ? (ind.bullish ? "BULLISH ✔" : "BEARISH ✔") : "CLOSED"}
        };
        for (int i = 0; i < rows.length; i++) {
            Label lName = new Label(rows[i][0]);
            lName.setStyle("-fx-text-fill: #aaa; -fx-font-size: 11px;");
            Label lVal = new Label(rows[i][1]);
            boolean ok = checks[i < checks.length ? i : checks.length-1];
            lVal.setStyle("-fx-text-fill: " + (ok ? "#39aa39" : "#c93535") + "; -fx-font-size: 11px;");
            grid.add(lName, 0, i);
            grid.add(lVal,  1, i);
        }

        // BN indicator sub-section
        if (ind != null) {
            Separator sep = new Separator();
            sep.setStyle("-fx-background-color: #333;");
            panelEntryLoop.getChildren().addAll(grid, sep);

            Label indHead = new Label("BN Indicators  Bull:" + String.format("%.1f", ind.bull)
                + "  Bear:" + String.format("%.1f", ind.bear));
            indHead.setStyle("-fx-text-fill: #a78bfa; -fx-font-size: 12px;");
            panelEntryLoop.getChildren().add(indHead);

            if (ind.rsi != null) {
                Label rsiLbl = new Label("RSI(14): " + ind.rsi);
                String rc = ind.rsi < 35 ? "#39aa39" : ind.rsi > 65 ? "#c93535" : "#ddd";
                rsiLbl.setStyle("-fx-text-fill: " + rc + "; -fx-font-size: 11px;");
                panelEntryLoop.getChildren().add(rsiLbl);
            }
            if (ind.emaStack != null) {
                String emaStr = (ind.emaStack.bullish ? "▲" : ind.emaStack.bearish ? "▼" : "~")
                    + " EMA20=" + ind.emaStack.ema20 + " EMA50=" + ind.emaStack.ema50;
                Label emaLbl = new Label(emaStr);
                String ec = ind.emaStack.bullish ? "#39aa39" : ind.emaStack.bearish ? "#c93535" : "#888";
                emaLbl.setStyle("-fx-text-fill: " + ec + "; -fx-font-size: 11px;");
                panelEntryLoop.getChildren().add(emaLbl);
            }
        } else {
            panelEntryLoop.getChildren().add(grid);
        }

        // Leader stocks sub-table
        if (!d.stocks().isEmpty()) {
            Separator sep2 = new Separator();
            panelEntryLoop.getChildren().add(sep2);
            Label hdr = new Label("Leader Stocks");
            hdr.setStyle("-fx-text-fill: #888; -fx-font-size: 11px;");
            panelEntryLoop.getChildren().add(hdr);
            for (AppState.StockStat ss : d.stocks()) {
                if (ss.candle() == null) continue;
                double diff = ss.candle().close - ss.candle().open;
                String dir = diff > 0 ? "▲" : diff < 0 ? "▼" : "—";
                String sc  = diff > 0 ? "#39aa39" : diff < 0 ? "#c93535" : "#888";
                Label sl = new Label(String.format("%-22s  %s  qty=%.0f thr=%.0f",
                    ss.stock(), dir, ss.qty(), ss.threshold()));
                sl.setStyle("-fx-text-fill: " + sc + "; -fx-font-size: 11px; -fx-font-family: monospace;");
                panelEntryLoop.getChildren().add(sl);
            }
        }
    }

    // ── ATM options panel ─────────────────────────────────────────────────────

    private void updateAtmPanel() {
        AtmOption opt = state.atmOption;
        if (panelAtmOption == null) return;
        panelAtmOption.getChildren().clear();
        if (opt == null) {
            panelAtmOption.getChildren().add(styledLabel("No active option position", "#555"));
            return;
        }
        double iv     = OptionsEngine.estimateVol() * 100;
        double dte    = OptionsEngine.timeToExpiry() * 365;
        double pnlPts = opt.pnlPts();
        double pnlRs  = opt.pnlRs();
        String pColor = pnlRs >= 0 ? "#4caf50" : "#c93535";

        panelAtmOption.getChildren().addAll(
            styledLabel(String.format("BANKNIFTY %d %s  Expiry: %s", opt.strike, opt.type, opt.expiryDate), "#38bdf8"),
            styledLabel(String.format("Entry ₹%.2f  →  Live ₹%.2f  Cost ₹%.0f",
                opt.entryPremium, opt.currentPremium, opt.entryCost()), "#ddd"),
            styledLabel(String.format("P&L pts: %+.2f   P&L ₹: %+.0f", pnlPts, pnlRs), pColor),
            styledLabel(String.format("Delta %.3f   Theta/day ₹%.1f   IV %.1f%%   DTE %.2fd",
                opt.delta, opt.theta * AppConfig.LOT_SIZE, iv, dte), "#aaa")
        );

        // Manual IV override
        HBox ivBox = new HBox(6);
        TextField ivField = new TextField(state.manualIV != null ? String.format("%.1f", state.manualIV*100) : "");
        ivField.setPromptText("IV%");
        ivField.setPrefWidth(60);
        ivField.setStyle("-fx-background-color: #1a1a2e; -fx-text-fill: #f6c453; -fx-border-color: #333;");
        Button ivApply = new Button("Apply IV");
        ivApply.setOnAction(e -> {
            try {
                double v = Double.parseDouble(ivField.getText());
                if (v >= 5 && v <= 200) state.manualIV = v / 100;
            } catch (NumberFormatException ignored) {}
        });
        Button ivClear = new Button("Clear");
        ivClear.setOnAction(e -> state.manualIV = null);
        ivBox.getChildren().addAll(new Label("IV:"), ivField, ivApply, ivClear);
        ivBox.setStyle("-fx-padding: 4 0 0 0;");
        panelAtmOption.getChildren().add(ivBox);
    }

    private Label styledLabel(String text, String color) {
        Label l = new Label(text);
        l.setStyle("-fx-text-fill: " + color + "; -fx-font-size: 12px;");
        return l;
    }

    // ── S/R panel ─────────────────────────────────────────────────────────────

    private void updateSRPanel() {
        if (panelSRZone == null) return;
        panelSRZone.getChildren().clear();
        AppState.SRLevels sr = state.srLevels.get(AppConfig.INDEX_SYMBOL);
        if (sr == null) {
            panelSRZone.getChildren().add(styledLabel("S/R not loaded", "#555"));
            return;
        }
        panelSRZone.getChildren().add(styledLabel("BANKNIFTY Support / Resistance (5m)", "#4caf50"));
        if (!sr.supports().isEmpty()) {
            String s = "Support:    " + sr.supports().stream()
                .map(v -> String.format("%.2f", v)).reduce((a, b) -> a + "  " + b).orElse("—");
            panelSRZone.getChildren().add(styledLabel(s, "#39aa39"));
        }
        if (!sr.resistances().isEmpty()) {
            String r = "Resistance: " + sr.resistances().stream()
                .map(v -> String.format("%.2f", v)).reduce((a, b) -> a + "  " + b).orElse("—");
            panelSRZone.getChildren().add(styledLabel(r, "#c93535"));
        }
    }

    // ── Trades panel ──────────────────────────────────────────────────────────

    private void buildTradesTable() {
        String[][] cols = {
            {"#","40"}, {"Type","55"}, {"Entry","65"}, {"OptEntry","70"},
            {"Exit","65"}, {"OptExit","70"}, {"SL","60"}, {"Target","65"},
            {"Conf","60"}, {"P&L pts","65"}, {"Opt P&L ₹","75"}, {"Status","65"}
        };
        for (String[] c : cols) {
            TableColumn<TradeRow, String> col = new TableColumn<>(c[0]);
            String prop = c[0];
            col.setCellValueFactory(cd -> new SimpleStringProperty(cd.getValue().get(prop)));
            col.setPrefWidth(Double.parseDouble(c[1]));
            tableTrades.getColumns().add(col);
        }
        tableTrades.setPlaceholder(new Label("No trades recorded"));
        styleTable(tableTrades);
    }

    private void updateTradesPanel() {
        List<Trade> all = dashboardAll ? DatabaseService.getAllTrades() : DatabaseService.getTodayTrades();
        // Pair entry + exit
        List<TradePair> pairs = pairTrades(all);

        ObservableList<TradeRow> rows = FXCollections.observableArrayList();
        double closedPnl = 0;
        int wins = 0, losses = 0;
        int n = 1;

        for (TradePair p : pairs) {
            Trade e = p.entry;
            Trade ex = p.exit;
            boolean isOpen = ex == null && state.activeTrade != null;

            double slPrice, tgtPrice;
            if (e.type.equals("BUY")) {
                slPrice  = e.price - AppConfig.STOPLOSS;
                tgtPrice = e.price + AppConfig.TARGET;
            } else {
                slPrice  = e.price + AppConfig.STOPLOSS;
                tgtPrice = e.price - AppConfig.TARGET;
            }

            List<Candle> bnC = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
            double livePrice = (bnC != null && !bnC.isEmpty()) ? bnC.get(bnC.size()-1).close : 0;

            double pnl;
            String exitStr, status, pnlColor;
            if (isOpen && p == pairs.get(pairs.size()-1)) {
                exitStr = String.format("%.2f*", livePrice);
                pnl = e.type.equals("BUY") ? livePrice - e.price : e.price - livePrice;
                if (state.activeTrade != null) slPrice = state.activeTrade.currentSL;
                status   = "LIVE";
                pnlColor = pnl >= 0 ? "#4caf50" : "#c93535";
            } else if (ex != null) {
                exitStr = String.format("%.2f", ex.price);
                pnl     = ex.pnl;
                closedPnl += pnl;
                if (pnl > 0) wins++; else losses++;
                status   = pnl >= 0 ? "WIN" : "LOSS";
                pnlColor = pnl >= 0 ? "#4caf50" : "#c93535";
            } else {
                exitStr = "—"; pnl = 0; status = "—"; pnlColor = "#888";
            }

            // Option P&L
            String optEntry = e.optionPremium != null ? String.format("₹%.1f", e.optionPremium) : "—";
            String optExit  = "—";
            String optPnl   = "—";
            AtmOption opt = state.atmOption;
            if (ex != null && ex.optionPremium != null && e.optionPremium != null) {
                double ops = (ex.optionPremium - e.optionPremium) * AppConfig.LOT_SIZE;
                optExit = String.format("₹%.1f", ex.optionPremium);
                optPnl  = String.format("%+.0f", ops);
            } else if (isOpen && opt != null) {
                optExit = String.format("₹%.1f*", opt.currentPremium);
                optPnl  = String.format("%+.0f", opt.pnlRs());
            }

            TradeRow row = new TradeRow();
            row.put("#",        String.valueOf(n++));
            row.put("Type",     e.type);
            row.put("Entry",    String.format("%.2f", e.price));
            row.put("OptEntry", optEntry);
            row.put("Exit",     exitStr);
            row.put("OptExit",  optExit);
            row.put("SL",       String.format("%.1f", slPrice));
            row.put("Target",   String.format("%.1f", tgtPrice));
            row.put("Conf",     e.confidence != null ? e.confidence : "—");
            row.put("P&L pts",  pnl != 0 ? String.format("%+.2f", pnl) : "—");
            row.put("Opt P&L ₹", optPnl);
            row.put("Status",   status);
            rows.add(row);
        }
        tableTrades.setItems(rows);

        int closed = wins + losses;
        String wr = closed > 0 ? String.format("%.0f%%", wins * 100.0 / closed) : "—";
        labelTradeSummary.setText(String.format(
            "Trades: %d  |  Win: %d  Loss: %d  |  Win Rate: %s  |  P&L: %+.2f pts",
            pairs.size(), wins, losses, wr, closedPnl));
    }

    private List<TradePair> pairTrades(List<Trade> all) {
        List<TradePair> paired = new ArrayList<>();
        TradePair pending = null;
        for (Trade t : all) {
            if (!t.type.contains("EXIT")) {
                pending = new TradePair(t, null);
            } else if (pending != null) {
                pending.exit = t;
                paired.add(pending);
                pending = null;
            }
        }
        if (pending != null) paired.add(pending);
        return paired;
    }

    // ── Active trade label ────────────────────────────────────────────────────

    private void updateActiveTrade() {
        ActiveTrade at = state.activeTrade;
        List<Candle> bnC = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        double livePrice = (bnC != null && !bnC.isEmpty()) ? bnC.get(bnC.size()-1).close : 0;

        if (at == null) {
            labelActiveTrade.setText("No active trade");
            labelActiveTrade.setStyle("-fx-text-fill: #555;");
            btnExit.setDisable(true);
        } else {
            double pnl = at.type.equals("BUY") ? livePrice - at.entry : at.entry - livePrice;
            String pColor = pnl >= 0 ? "#4caf50" : "#c93535";
            String typeColor = at.type.equals("BUY") ? "#39aa39" : "#c93535";
            labelActiveTrade.setText(String.format(
                "%s @ %.2f  SL=%.1f  P&L: %+.2f", at.type, at.entry, at.currentSL, pnl));
            labelActiveTrade.setStyle("-fx-text-fill: " + pColor + "; -fx-font-weight: bold;");
            btnExit.setDisable(false);
        }
    }

    private void updateFunds() {
        labelFunds.setText(String.format("Funds: ₹%.0f", state.availableFunds));
    }

    // ── Order form actions ────────────────────────────────────────────────────

    @FXML
    private void onBuy() {
        if (state.activeTrade != null) { showAlert("A trade is already active."); return; }
        double price = getOrderPrice();
        if (price <= 0) { showAlert("Live price not available."); return; }
        tradeEngine.manualEntry("BUY", price);
        updateActiveTrade();
    }

    @FXML
    private void onSell() {
        if (state.activeTrade != null) { showAlert("A trade is already active."); return; }
        double price = getOrderPrice();
        if (price <= 0) { showAlert("Live price not available."); return; }
        tradeEngine.manualEntry("SELL", price);
        updateActiveTrade();
    }

    @FXML
    private void onExit() {
        tradeEngine.manualExit();
        updateActiveTrade();
    }

    @FXML
    private void onClearTrades() {
        Alert confirm = new Alert(Alert.AlertType.CONFIRMATION, "Clear ALL trades?", ButtonType.YES, ButtonType.NO);
        confirm.showAndWait().ifPresent(bt -> {
            if (bt == ButtonType.YES) {
                DatabaseService.clearAllTrades();
                state.activeTrade  = null;
                state.lastExitTime = 0;
                OptionsEngine.stopATMTracking();
                updateTradesPanel();
                updateActiveTrade();
            }
        });
    }

    @FXML
    private void onFilterToday() {
        dashboardAll = false;
        updateTradesPanel();
    }

    @FXML
    private void onFilterAll() {
        dashboardAll = true;
        updateTradesPanel();
    }

    @FXML
    private void onRefreshSR() {
        MainScheduler.execute(() -> {
            BreakoutEngine.SRLevels sr = BreakoutEngine.detectSupportResistance(
                state.bnIndicatorCandles.isEmpty()
                    ? state.lastNCandles.getOrDefault(AppConfig.INDEX_SYMBOL, Collections.emptyList())
                    : state.bnIndicatorCandles);
            state.srLevels.put(AppConfig.INDEX_SYMBOL,
                new AppState.SRLevels(sr.supports(), sr.resistances()));
            Platform.runLater(this::updateSRPanel);
        });
    }

    private double getOrderPrice() {
        if ("MARKET".equals(comboOrderType.getValue())) {
            List<Candle> bnC = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
            return (bnC != null && !bnC.isEmpty()) ? bnC.get(bnC.size()-1).close : 0;
        } else {
            try { return Double.parseDouble(fieldOrderPrice.getText()); }
            catch (NumberFormatException e) { return 0; }
        }
    }

    private void showAlert(String msg) {
        Alert a = new Alert(Alert.AlertType.INFORMATION, msg, ButtonType.OK);
        a.showAndWait();
    }

    // ── IST Analog Clock (Canvas) ─────────────────────────────────────────────

    private void startClockTimer() {
        MainScheduler.scheduleAtFixedRate(
            () -> Platform.runLater(this::drawClock), 0, 100, TimeUnit.MILLISECONDS);
    }

    private void drawClock() {
        if (clockCanvas == null) return;
        GraphicsContext gc = clockCanvas.getGraphicsContext2D();
        double w = clockCanvas.getWidth();
        double h = clockCanvas.getHeight();
        double cx = w / 2, cy = h / 2;
        double r  = Math.min(w, h) / 2 - 4;

        gc.clearRect(0, 0, w, h);

        // Face
        gc.setFill(Color.web("#0f172a"));
        gc.fillOval(cx - r, cy - r, r * 2, r * 2);
        gc.setStroke(Color.web("#222"));
        gc.setLineWidth(3);
        gc.strokeOval(cx - r, cy - r, r * 2, r * 2);

        LocalDateTime ist = LocalDateTime.now(ZoneId.of("Asia/Kolkata"));
        int hours = ist.getHour(), mins = ist.getMinute(), secs = ist.getSecond(), ms = ist.getNano() / 1_000_000;

        double secAngle  = (secs + ms / 1000.0) * 6;
        double minAngle  = (mins + secs / 60.0) * 6;
        double hourAngle = ((hours % 12) + mins / 60.0 + secs / 3600.0) * 30;

        drawHand(gc, cx, cy, r * 0.55, hourAngle, 4, "#38bdf8");
        drawHand(gc, cx, cy, r * 0.75, minAngle,  3, "#38bdf8");
        drawHand(gc, cx, cy, r * 0.85, secAngle,  1.5, "#38bdf8");

        // Center cap
        gc.setFill(Color.web("#38bdf8"));
        gc.fillOval(cx - 4, cy - 4, 8, 8);

        // Digital time
        if (labelTime != null) {
            labelTime.setText(String.format("%02d:%02d:%02d IST", hours, mins, secs));
        }
    }

    private void drawHand(GraphicsContext gc, double cx, double cy,
                           double length, double angleDeg, double width, String colorHex) {
        double rad = Math.toRadians(angleDeg - 90);
        double ex  = cx + length * Math.cos(rad);
        double ey  = cy + length * Math.sin(rad);
        gc.setStroke(Color.web(colorHex));
        gc.setLineWidth(width);
        gc.strokeLine(cx, cy, ex, ey);
    }

    // ── Table style helper ────────────────────────────────────────────────────

    private void styleTable(TableView<?> tv) {
        tv.setStyle("-fx-background-color: #1e1e1e; -fx-text-fill: #eee;");
        tv.setColumnResizePolicy(TableView.CONSTRAINED_RESIZE_POLICY_FLEX_LAST_COLUMN);
    }

    // ── Row model classes ─────────────────────────────────────────────────────

    public static class CandleRow {
        String symbol;
        String name;
        String buyQty  = "N/A";
        String sellQty = "N/A";
        Map<Integer, String> candleValues = new LinkedHashMap<>();
    }

    public static class BigTradeRow {
        final int index;
        BigTradeRow(int i) { this.index = i; }
    }

    public static class TradeRow {
        private final Map<String, String> data = new LinkedHashMap<>();
        void put(String k, String v) { data.put(k, v); }
        String get(String k) { return data.getOrDefault(k, "—"); }
    }

    public static class TradePair {
        Trade entry; Trade exit;
        TradePair(Trade e, Trade x) { entry = e; exit = x; }
    }
}
