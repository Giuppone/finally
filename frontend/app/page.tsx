"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AnalyticsPanel, type AnalyticsTab } from "@/components/AnalyticsPanel";
import { ChatPanel } from "@/components/ChatPanel";
import { Header } from "@/components/Header";
import { Heatmap } from "@/components/Heatmap";
import { MainChart } from "@/components/MainChart";
import { PnlChart } from "@/components/PnlChart";
import { PositionsTable } from "@/components/PositionsTable";
import type { ChartRange } from "@/components/RangeSelector";
import { TradeBar } from "@/components/TradeBar";
import { Watchlist } from "@/components/Watchlist";
import * as api from "@/lib/api";
import { derivePortfolio } from "@/lib/derive";
import { useStream } from "@/lib/useStream";
import type {
  ChatMessage,
  DailySeries,
  HistoryRange,
  Portfolio,
  PortfolioCurve,
  Side,
  SnapshotPoint,
  WatchlistEntry,
} from "@/lib/types";

/** Catches snapshot-driven drift; live prices arrive over SSE, not from here. */
const REFRESH_MS = 15_000;

export default function Terminal() {
  const { quotes, series, mode, healthy, connection, seedSeries } = useStream();

  const [entries, setEntries] = useState<WatchlistEntry[]>([]);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotPoint[]>([]);
  const [startingCash, setStartingCash] = useState(10_000);
  const [selected, setSelected] = useState<string | null>(null);

  // Daily history. Separate from `snapshots` above because it is a separate account: that is
  // the $10,000 paper book, this is the real one reconstructed from the dated ledger.
  const [curve, setCurve] = useState<PortfolioCurve | null>(null);
  const [pnlRange, setPnlRange] = useState<ChartRange>("max");
  const [basis, setBasis] = useState<"value" | "percent">("value");
  const [daily, setDaily] = useState<DailySeries | null>(null);
  const [chartRange, setChartRange] = useState<ChartRange>("live");
  const [historyLoading, setHistoryLoading] = useState(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [mock, setMock] = useState(false);

  const [analyticsOpen, setAnalyticsOpen] = useState(false);
  const [analyticsTab, setAnalyticsTab] = useState<AnalyticsTab>("risk");

  const [tradeTicker, setTradeTicker] = useState("");
  const [tradeQuantity, setTradeQuantity] = useState("");

  // Tickers already seeded from the API, so selecting a ticker twice does not refetch.
  const seeded = useRef<Set<string>>(new Set());
  // Daily series are derived from a committed artifact and never change while the process
  // lives, so they are fetched once per (key, range) and kept. Deliberately NOT on the 15s
  // refresh interval below — re-fetching immutable data every 15 seconds is pure waste.
  const curveCache = useRef<Map<string, PortfolioCurve>>(new Map());
  const dailyCache = useRef<Map<string, DailySeries | null>>(new Map());

  const refreshWatchlist = useCallback(async () => {
    const data = await api.getWatchlist();
    setEntries(data.tickers);
    setSelected((current) => current ?? data.tickers[0]?.ticker ?? null);
    return data.tickers;
  }, []);

  const refreshPortfolio = useCallback(async () => {
    const data = await api.getPortfolio();
    setPortfolio(data);
    setStartingCash(data.starting_cash);
  }, []);

  const refreshSnapshots = useCallback(async () => {
    const data = await api.getPortfolioHistory();
    setSnapshots(data.points);
    setStartingCash(data.starting_cash);
  }, []);

  const loadCurve = useCallback(async (range: HistoryRange) => {
    const cached = curveCache.current.get(range);
    if (cached) {
      setCurve(cached);
      return cached;
    }
    setHistoryLoading(true);
    try {
      const data = await api.getPortfolioCurve(range);
      curveCache.current.set(range, data);
      setCurve(data);
      return data;
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const loadDaily = useCallback(async (ticker: string, range: HistoryRange) => {
    const key = `${ticker}:${range}`;
    if (dailyCache.current.has(key)) {
      setDaily(dailyCache.current.get(key) ?? null);
      return;
    }
    setHistoryLoading(true);
    try {
      const data = await api.getDailyPrices(ticker, range);
      dailyCache.current.set(key, data);
      setDaily(data);
    } catch {
      // A 404 here is ordinary: a ticker added today has no daily bars. Cache the miss so
      // selecting it repeatedly does not re-ask, and let MainChart hide the daily ranges.
      dailyCache.current.set(key, null);
      setDaily(null);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  // ---- initial load ---------------------------------------------------------

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const tickers = await refreshWatchlist();
        await Promise.all([
          refreshPortfolio(),
          refreshSnapshots(),
          // The evolution is what the user asked to see on load, so it is fetched with the
          // first paint rather than on first interaction. Falls back to LIVE when the build
          // carries no reconstructed ledger.
          loadCurve("max")
            .then((data) => {
              if (!cancelled && !data.available) setPnlRange("live");
            })
            .catch(() => {
              if (!cancelled) setPnlRange("live");
            }),
        ]);

        // One round trip seeds every sparkline, rather than N per-ticker calls (§8).
        const symbols = tickers.map((entry) => entry.ticker);
        if (symbols.length && !cancelled) {
          const { series: seeds } = await api.getBulkHistory(symbols);
          symbols.forEach((symbol) => seeded.current.add(symbol));
          seedSeries(seeds);
        }

        const chat = await api.getChatHistory();
        if (!cancelled) {
          setMessages(chat.messages);
          setMock(chat.mock);
        }
      } catch {
        // The stream and the periodic refresh both recover on their own; a failed first
        // paint should not leave a dead page.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [refreshWatchlist, refreshPortfolio, refreshSnapshots, seedSeries, loadCurve]);

  useEffect(() => {
    const timer = setInterval(() => {
      void refreshPortfolio().catch(() => {});
      void refreshSnapshots().catch(() => {});
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshPortfolio, refreshSnapshots]);

  // Re-fetch on a deliberate control change, following the AnalyticsPanel effect pattern.
  useEffect(() => {
    if (pnlRange === "live") return;
    void loadCurve(pnlRange).catch(() => {});
  }, [pnlRange, loadCurve]);

  // Daily closes for the selected ticker. Fetched for the LIVE range too, because the range
  // strip can only offer the daily options once we know whether this ticker has any bars.
  useEffect(() => {
    if (!selected) {
      setDaily(null);
      return;
    }
    void loadDaily(selected, chartRange === "live" ? "max" : chartRange).catch(() => {});
  }, [selected, chartRange, loadDaily]);

  // Deeper history for whatever is in the main chart, fetched once per ticker.
  useEffect(() => {
    if (!selected || seeded.current.has(`full:${selected}`)) return;
    seeded.current.add(`full:${selected}`);
    void api
      .getHistory(selected)
      .then(({ points }) => seedSeries({ [selected]: points }))
      .catch(() => {});
  }, [selected, seedSeries]);

  // ---- actions --------------------------------------------------------------

  const handleAdd = useCallback(
    async (ticker: string) => {
      await api.addTicker(ticker);
      const tickers = await refreshWatchlist();
      setSelected(ticker);
      const fresh = tickers.map((entry) => entry.ticker).filter((t) => !seeded.current.has(t));
      if (fresh.length) {
        const { series: seeds } = await api.getBulkHistory(fresh);
        fresh.forEach((symbol) => seeded.current.add(symbol));
        seedSeries(seeds);
      }
    },
    [refreshWatchlist, seedSeries],
  );

  const handleRemove = useCallback(
    async (ticker: string) => {
      await api.removeTicker(ticker);
      const tickers = await refreshWatchlist();
      setSelected((current) =>
        current === ticker ? (tickers[0]?.ticker ?? null) : current,
      );
    },
    [refreshWatchlist],
  );

  const handleTrade = useCallback(
    async (ticker: string, quantity: number, side: Side) => {
      const { trade, portfolio: fresh } = await api.trade(ticker, quantity, side);
      setPortfolio(fresh);
      // A buy can add a ticker to the watchlist, and a full sell can drop one from the
      // tracked set — both change the grid.
      await Promise.all([refreshWatchlist(), refreshSnapshots()]);
      return trade;
    },
    [refreshWatchlist, refreshSnapshots],
  );

  const handleSend = useCallback(
    async (text: string) => {
      setPending(text);
      setSending(true);
      setChatError(null);
      try {
        const response = await api.sendChat(text);
        setMessages((previous) => [
          ...previous,
          {
            id: `local-${Date.now()}`,
            role: "user",
            content: text,
            actions: [],
            created_at: new Date().toISOString(),
          },
          response.message,
        ]);
        if (response.portfolio) setPortfolio(response.portfolio);
        if (response.message.actions.length) {
          await Promise.all([refreshWatchlist(), refreshSnapshots()]);
        }
      } catch (caught) {
        setChatError(
          caught instanceof Error ? caught.message : "The assistant is unavailable.",
        );
      } finally {
        setPending(null);
        setSending(false);
      }
    },
    [refreshWatchlist, refreshSnapshots],
  );

  const handleReset = useCallback(async () => {
    await api.resetPortfolio();
    seeded.current.clear();
    setMessages([]);
    setSnapshots([]);
    await Promise.all([refreshWatchlist(), refreshPortfolio(), refreshSnapshots()]);
  }, [refreshWatchlist, refreshPortfolio, refreshSnapshots]);

  const selectTicker = useCallback((ticker: string) => {
    setSelected(ticker);
    setTradeTicker(ticker);
  }, []);

  const live = useMemo(() => derivePortfolio(portfolio, quotes), [portfolio, quotes]);

  return (
    <div className="flex h-screen flex-col gap-2 bg-base p-2">
      <Header
        portfolio={live}
        mode={mode}
        connection={connection}
        healthy={healthy}
        onReset={() => void handleReset()}
      />

      <div className="grid min-h-0 flex-1 grid-cols-[290px_minmax(0,1fr)_330px] gap-2">
        <Watchlist
          entries={entries}
          quotes={quotes}
          series={series}
          selected={selected}
          onSelect={selectTicker}
          onAdd={handleAdd}
          onRemove={(ticker) => void handleRemove(ticker)}
        />

        <div className="grid min-h-0 grid-rows-[minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,0.95fr)] gap-2">
          <MainChart
            ticker={selected}
            points={selected ? (series[selected] ?? []) : []}
            quote={selected ? quotes[selected] : undefined}
            daily={daily}
            range={chartRange}
            onRangeChange={setChartRange}
            loading={historyLoading}
          />

          <div className="grid min-h-0 grid-cols-2 gap-2">
            <Heatmap positions={live?.positions ?? []} />
            <PnlChart
              points={snapshots}
              startingCash={startingCash}
              curve={curve}
              range={pnlRange}
              onRangeChange={setPnlRange}
              basis={basis}
              onBasisChange={setBasis}
              loading={historyLoading}
            />
          </div>

          <PositionsTable
            positions={live?.positions ?? []}
            selected={selected}
            onSelect={selectTicker}
            onAnalyze={(tab) => {
              setAnalyticsTab(tab);
              setAnalyticsOpen(true);
            }}
          />
        </div>

        <ChatPanel
          messages={messages}
          pending={pending}
          sending={sending}
          error={chatError}
          mock={mock}
          onSend={(text) => void handleSend(text)}
        />
      </div>

      <AnalyticsPanel
        open={analyticsOpen}
        tab={analyticsTab}
        positions={live?.positions ?? []}
        watchlist={entries.map((entry) => entry.ticker)}
        onTab={setAnalyticsTab}
        onClose={() => setAnalyticsOpen(false)}
        onApplied={() => {
          void refreshPortfolio().catch(() => {});
          void refreshSnapshots().catch(() => {});
        }}
      />

      <TradeBar
        ticker={tradeTicker}
        quantity={tradeQuantity}
        quotes={quotes}
        cash={live?.cash_balance}
        onTickerChange={setTradeTicker}
        onQuantityChange={setTradeQuantity}
        onTrade={handleTrade}
      />
    </div>
  );
}
