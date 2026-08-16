"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ChatPanel } from "@/components/ChatPanel";
import { Header } from "@/components/Header";
import { Heatmap } from "@/components/Heatmap";
import { MainChart } from "@/components/MainChart";
import { PnlChart } from "@/components/PnlChart";
import { PositionsTable } from "@/components/PositionsTable";
import { TradeBar } from "@/components/TradeBar";
import { Watchlist } from "@/components/Watchlist";
import * as api from "@/lib/api";
import { derivePortfolio } from "@/lib/derive";
import { useStream } from "@/lib/useStream";
import type {
  ChatMessage,
  Portfolio,
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

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [mock, setMock] = useState(false);

  const [tradeTicker, setTradeTicker] = useState("");
  const [tradeQuantity, setTradeQuantity] = useState("");

  // Tickers already seeded from the API, so selecting a ticker twice does not refetch.
  const seeded = useRef<Set<string>>(new Set());

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

  // ---- initial load ---------------------------------------------------------

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const tickers = await refreshWatchlist();
        await Promise.all([refreshPortfolio(), refreshSnapshots()]);

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
  }, [refreshWatchlist, refreshPortfolio, refreshSnapshots, seedSeries]);

  useEffect(() => {
    const timer = setInterval(() => {
      void refreshPortfolio().catch(() => {});
      void refreshSnapshots().catch(() => {});
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshPortfolio, refreshSnapshots]);

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
          />

          <div className="grid min-h-0 grid-cols-2 gap-2">
            <Heatmap positions={live?.positions ?? []} />
            <PnlChart points={snapshots} startingCash={startingCash} />
          </div>

          <PositionsTable
            positions={live?.positions ?? []}
            selected={selected}
            onSelect={selectTicker}
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
