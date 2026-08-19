"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ConnectionState,
  Mode,
  PricePoint,
  Quote,
  StreamFrame,
} from "./types";

/** Points retained per ticker in memory. ~5 min at the 500ms cadence; sparklines need 60. */
const MAX_POINTS = 600;

export interface StreamState {
  quotes: Record<string, Quote>;
  /** Accumulated (ts, price) per ticker: API-seeded, then extended from the stream. */
  series: Record<string, PricePoint[]>;
  mode: Mode | null;
  healthy: boolean;
  connection: ConnectionState;
  /** Seeds history fetched from the API so charts render populated on first paint. */
  seedSeries: (seeded: Record<string, PricePoint[]>) => void;
}

/**
 * Subscribes to `GET /api/stream/prices`.
 *
 * The backend emits UNNAMED `data:` frames discriminated by a `type` field, so this reads
 * `onmessage` and switches. `addEventListener("prices", …)` would silently receive nothing
 * — see backend/app/market/routes.py.
 *
 * EventSource reconnects on its own (the server sends `retry: 1000`); the manual restart
 * below only covers the case where the browser gives up entirely and parks at CLOSED.
 */
export function useStream(): StreamState {
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [series, setSeries] = useState<Record<string, PricePoint[]>>({});
  const [mode, setMode] = useState<Mode | null>(null);
  const [healthy, setHealthy] = useState(true);
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  // Whether we have ever been connected, so the first attempt reads "connecting" and a
  // later drop reads "reconnecting" — the yellow dot means something different from grey.
  const everOpen = useRef(false);

  const appendTicks = useCallback((incoming: Quote[]) => {
    setSeries((previous) => {
      const next = { ...previous };
      for (const quote of incoming) {
        const points = next[quote.ticker] ?? [];
        const last = points[points.length - 1];
        // The stream re-sends every tracked ticker each frame whether or not it moved;
        // appending unconditionally would pack the buffer with duplicate timestamps.
        if (last && last.ts === quote.ts) continue;
        const grown = [...points, { ts: quote.ts, price: quote.price }];
        next[quote.ticker] =
          grown.length > MAX_POINTS ? grown.slice(grown.length - MAX_POINTS) : grown;
      }
      return next;
    });
  }, []);

  const seedSeries = useCallback((seeded: Record<string, PricePoint[]>) => {
    setSeries((previous) => {
      const next = { ...previous };
      for (const [ticker, points] of Object.entries(seeded)) {
        if (!points.length) continue;
        // Only seed what the stream has not already filled: live ticks are fresher, and
        // overwriting them mid-session would visibly rewind the sparkline.
        if ((next[ticker]?.length ?? 0) >= points.length) continue;
        next[ticker] = points.slice(-MAX_POINTS);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      if (closed) return;
      source = new EventSource("/api/stream/prices");

      source.onopen = () => {
        everOpen.current = true;
        setConnection("open");
      };

      source.onmessage = (event: MessageEvent<string>) => {
        let frame: StreamFrame;
        try {
          frame = JSON.parse(event.data) as StreamFrame;
        } catch {
          return; // a keepalive comment never reaches onmessage, but be defensive
        }

        if (frame.type === "hello") {
          setMode(frame.mode);
        }
        setHealthy(frame.healthy);
        setQuotes((previous) => {
          const next = { ...previous };
          for (const quote of frame.quotes) next[quote.ticker] = quote;
          // A ticker dropped from the tracked set must disappear from the grid rather than
          // linger at a frozen price.
          const live = new Set(frame.quotes.map((q) => q.ticker));
          for (const ticker of Object.keys(next)) {
            if (!live.has(ticker)) delete next[ticker];
          }
          return next;
        });
        appendTicks(frame.quotes);
      };

      source.onerror = () => {
        setConnection(everOpen.current ? "reconnecting" : "connecting");
        // readyState CONNECTING means the browser is already retrying — leave it alone.
        if (source && source.readyState === EventSource.CLOSED) {
          source.close();
          setConnection("closed");
          retry = setTimeout(connect, 2000);
        }
      };
    };

    connect();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      source?.close();
    };
  }, [appendTicks]);

  return { quotes, series, mode, healthy, connection, seedSeries };
}
