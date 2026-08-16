"use client";

import { useState } from "react";

import { DASH, money, percent, toneClass } from "@/lib/format";
import type { PricePoint, Quote, WatchlistEntry } from "@/lib/types";

import { Sparkline } from "./Sparkline";

function Row({
  entry,
  quote,
  points,
  selected,
  onSelect,
  onRemove,
}: {
  entry: WatchlistEntry;
  quote: Quote | undefined;
  points: PricePoint[];
  selected: boolean;
  onSelect: () => void;
  onRemove: () => void;
}) {
  // A watchlist row is priced if EITHER source has it: the stream is fresher, but the REST
  // payload covers the gap before the first frame arrives.
  const price = quote?.price ?? entry.price;
  const changePct = quote?.change_pct ?? entry.change_pct;
  const priced = price != null;

  // Flashing by remounting on `ts` rather than juggling a timer: re-adding an identical
  // class does not restart a CSS animation, so a state-based flash silently stops firing
  // when consecutive ticks share a direction. A keyed span always restarts.
  const flash =
    quote?.direction === "up"
      ? "flash-up"
      : quote?.direction === "down"
        ? "flash-down"
        : "";

  return (
    <tr
      onClick={onSelect}
      data-testid={`watchlist-row-${entry.ticker}`}
      className={`group cursor-pointer border-b border-edge/60 last:border-b-0 ${
        selected ? "bg-brand/10" : "hover:bg-raised"
      }`}
    >
      <td className="py-1.5 pl-3 pr-2">
        <span
          className={`text-xs font-semibold tracking-wide ${
            selected ? "text-brand" : "text-ink"
          }`}
        >
          {entry.ticker}
        </span>
      </td>

      <td className="px-2 text-right">
        <span key={quote?.ts ?? "seed"} className={`tnum block text-xs ${flash}`}>
          {priced ? money(price) : DASH}
        </span>
      </td>

      <td className={`px-2 text-right tnum text-xs ${toneClass(changePct)}`}>
        {priced ? percent(changePct) : DASH}
      </td>

      <td className="px-1 py-1">
        <Sparkline points={points} />
      </td>

      <td className="pr-2 text-right">
        <button
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
          title={`Remove ${entry.ticker}`}
          aria-label={`Remove ${entry.ticker}`}
          className="px-1 text-sm leading-none text-faint opacity-0 transition-opacity
                     hover:text-down group-hover:opacity-100"
        >
          ×
        </button>
      </td>
    </tr>
  );
}

export function Watchlist({
  entries,
  quotes,
  series,
  selected,
  onSelect,
  onAdd,
  onRemove,
}: {
  entries: WatchlistEntry[];
  quotes: Record<string, Quote>;
  series: Record<string, PricePoint[]>;
  selected: string | null;
  onSelect: (ticker: string) => void;
  onAdd: (ticker: string) => Promise<void>;
  onRemove: (ticker: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const ticker = draft.trim().toUpperCase();
    if (!ticker || busy) return;

    setBusy(true);
    setError(null);
    try {
      await onAdd(ticker);
      setDraft("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not add that ticker");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>Watchlist</span>
        <span className="tnum text-faint">{entries.length}</span>
      </div>

      {/* x-hidden explicitly: the sparkline column has a fixed pixel width, so any rounding
          against the panel's own width would otherwise hang a horizontal scrollbar under a
          list that has nothing to scroll to. */}
      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
        <table className="w-full table-fixed border-collapse">
          {/* table-fixed needs the widths declared, or five columns simply split evenly and
              the sparkline's fixed 76px overflows its share. */}
          <colgroup>
            <col style={{ width: "50px" }} />
            <col style={{ width: "68px" }} />
            <col style={{ width: "52px" }} />
            <col style={{ width: "80px" }} />
            <col style={{ width: "20px" }} />
          </colgroup>
          <tbody>
            {entries.map((entry) => (
              <Row
                key={entry.ticker}
                entry={entry}
                quote={quotes[entry.ticker]}
                points={series[entry.ticker] ?? []}
                selected={selected === entry.ticker}
                onSelect={() => onSelect(entry.ticker)}
                onRemove={() => onRemove(entry.ticker)}
              />
            ))}
            {entries.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-xs text-faint">
                  Watchlist is empty. Add a ticker below.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <form onSubmit={submit} className="border-t border-edge p-2">
        <div className="flex gap-1.5">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Add ticker"
            aria-label="Add ticker to watchlist"
            maxLength={12}
            className="field w-full text-xs uppercase"
          />
          <button type="submit" disabled={busy || !draft.trim()} className="btn btn-submit">
            {busy ? "…" : "Add"}
          </button>
        </div>
        {error && <p className="mt-1.5 text-2xs text-down">{error}</p>}
      </form>
    </section>
  );
}
