# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Gradle + JavaFX 26** project (Kotlin DSL). The goal is to build a live stock trading dashboard — a JavaFX conversion of `original_code/c.html` — which displays live and historical BankNifty + constituent stock data, runs a trading engine with entry/exit logic, and tracks trades.

## Build & Run Commands

```bash
# Run the application
./gradlew run

# Build a distributable (runtime image + native installer)
./gradlew runtime        # creates build/image/
./gradlew jpackage       # creates a native installer

# Compile only
./gradlew compileJava

# Clean
./gradlew clean
```

There are no tests in this project yet. There is no lint step.

## Key Build Configuration

- **Gradle version:** 9.5.1 (see `gradle/wrapper/gradle-wrapper.properties`)
- **Java toolchain:** Java 26
- **JavaFX version:** 26 — modules `javafx.controls`, `javafx.fxml`
- **Entry point:** `org.example.hellofx.Launcher` (a thin wrapper that calls `HelloFX.main` — required so the fat-jar classpath can find the main class without the JavaFX module restrictions)
- **Plugins:** `org.openjfx.javafxplugin 0.1.0` (handles `--module-path` and `--add-modules` automatically), `org.beryx.runtime 2.0.1` (for `runtime`/`jpackage`)

## Architecture

### Package & Source Layout
All Java source lives under `src/main/java/org/example/hellofx/`. Resources (FXML, CSS, images) mirror that path under `src/main/resources/org/example/hellofx/`.

### JavaFX Pattern in Use
The project uses the **FXML + Controller** pattern:
- `HelloFX` — extends `Application`; loads `hellofx.fxml` via `FXMLLoader`, sets up the `Stage`
- `Launcher` — plain `main()` wrapper; avoids module-path issues when running from a non-modular classpath
- `Controller` — annotated with `@FXML`; wired to the FXML file via `fx:controller`
- `hellofx.fxml` — declares the scene graph; references `Controller` methods via `onAction="#methodName"`
- `hellofx.css` — loaded by the FXML via `stylesheets="@hellofx.css"`; uses JavaFX CSS (not browser CSS)

Any new top-level screens should follow this same triple: `*App.java` / `*.fxml` / `*Controller.java`.

### The Trading Dashboard (target)
`original_code/c.html` is the reference implementation. It uses:
- **REST API** at `https://34.100.254.34:8000/api/historical-data/` — POST with stock symbols + interval, returns OHLC candles keyed as `"5m data"`, `"1m data"`, etc.
- **WebSocket** at `ws://34.100.254.34:8083/historical-data` — live tick feed; each message is a JSON array of tick objects with fields `stock_symbol`, `stockname`, `interval`, `start_time`, `open`, `close`, `high`, `low`, `ltp` (string `"LTP 52050.00"`), `qty`, `snap` (string `"LTP ... BuyQty ... SellQty ..."`)
- **IndexedDB** (browser) — tick storage by stock/time/ltp/qty; replace with **SQLite** via `org.xerial:sqlite-jdbc`
- **Trading engine constants:** TARGET=35 pts, STOPLOSS=18 pts, BREAKEVEN_TRIGGER=12 pts, TRAIL_TRIGGER=18 pts, TRAIL_DISTANCE=12 pts, LOT_SIZE=30, SAME_DIRECTION_REQUIRED=3 of 6 leader stocks
- **Indicators:** RSI(14), MACD(12,26,9), EMA stack (20/50), candlestick patterns on BankNifty 5m candles; gate requires bull or bear score ≥ 2 with a 0.9-pt lead
- **UI layout:** Two-column split — left (Trade Dashboard + Big Trades), right (Entry Loop Monitor + Stock Candles); fixed IST clock top-right

When adding Jackson or SQLite, add them in `build.gradle.kts` under `dependencies { }` — the JavaFX plugin handles the JavaFX modules separately via the `javafx { }` block; do not add JavaFX JARs manually.

## Important Conventions

- **`--enable-native-access=ALL-UNNAMED`** is set as a default JVM arg in `build.gradle.kts`; required for JavaFX 26 on some platforms.
- The `runtime { }` block in `build.gradle.kts` strips debug info and compresses the native image; do not add `--add-reads` or `--patch-module` there without testing the runtime image.
- The API server uses HTTPS with a self-signed certificate; Java's `HttpClient` will need a trust-all `SSLContext` to connect.
