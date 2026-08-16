"use client";

import { useState } from "react";

import { money } from "@/lib/format";
import type { Quote, Side, TradeResult } from "@/lib/types";

export function TradeBar({
  ticker,
  quantity,
  quotes,
  cash,
  onTickerChange,
  onQuantityChange,
  onTrade,
}: {
  ticker: string;
  quantity: string;
  quotes: Record<string, Quote>;
  cash: number | undefined;
  onTickerChange: (value: string) => void;
  onQuantityChange: (value: string) => void;
  onTrade: (ticker: string, quantity: number, side: Side) => Promise<TradeResult>;
}) {
  const [busy, setBusy] = useState<Side | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const symbol = ticker.trim().toUpperCase();
  const parsed = Number.parseFloat(quantity);
  const valid = symbol.length > 0 && Number.isFinite(parsed) && parsed > 0;
  const price = quotes[symbol]?.price;
  const estimate = valid && price != null ? parsed * price : null;

  const submit = async (side: Side) => {
    if (!valid || busy) return;
    setBusy(side);
    setNotice(null);
    try {
      const result = await onTrade(symbol, parsed, side);
      setNotice({
        tone: "ok",
        text:
          `${side === "buy" ? "Bought" : "Sold"} ${result.quantity} ${result.ticker} ` +
          `@ ${money(result.fill_price)} = ${money(result.total)}`,
      });
    } catch (caught) {
      setNotice({
        tone: "bad",
        text: caught instanceof Error ? caught.message : "Trade failed",
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel flex items-center gap-3 px-3 py-2">
      <span className="text-2xs font-semibold uppercase tracking-[0.14em] text-muted">
        Trade
      </span>

      <input
        value={ticker}
        onChange={(event) => onTickerChange(event.target.value)}
        placeholder="TICKER"
        aria-label="Trade ticker"
        maxLength={12}
        data-testid="trade-ticker"
        className="field w-28 text-xs uppercase"
      />

      <input
        value={quantity}
        onChange={(event) => onQuantityChange(event.target.value)}
        placeholder="QTY"
        aria-label="Trade quantity"
        inputMode="decimal"
        data-testid="trade-quantity"
        className="field tnum w-24 text-xs"
      />

      <button
        onClick={() => submit("buy")}
        disabled={!valid || busy !== null}
        data-testid="trade-buy"
        className="btn btn-buy"
      >
        {busy === "buy" ? "…" : "Buy"}
      </button>
      <button
        onClick={() => submit("sell")}
        disabled={!valid || busy !== null}
        data-testid="trade-sell"
        className="btn btn-sell"
      >
        {busy === "sell" ? "…" : "Sell"}
      </button>

      {/* Market orders fill instantly at the live price, so the estimate is the fill to
          within one tick — worth showing before the click, not after. */}
      <span className="tnum text-2xs text-faint">
        {estimate != null
          ? `≈ ${money(estimate)} @ ${money(price)}`
          : symbol && price == null
            ? "no live price"
            : ""}
      </span>

      <div className="ml-auto flex items-center gap-3">
        {notice && (
          <span
            data-testid="trade-notice"
            className={`text-2xs ${notice.tone === "ok" ? "text-up" : "text-down"}`}
          >
            {notice.text}
          </span>
        )}
        <span className="tnum text-2xs text-faint">Cash {money(cash)}</span>
      </div>
    </section>
  );
}
