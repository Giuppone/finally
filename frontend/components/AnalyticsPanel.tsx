"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { OBJECTIVES, RebalancePreview } from "@/components/RebalancePreview";
import { RiskReport } from "@/components/RiskReport";
import * as api from "@/lib/api";
import type { LivePosition } from "@/lib/derive";
import type {
  HoldingInput,
  RebalanceObjective,
  RebalancePlan,
  RiskStats,
} from "@/lib/types";

export type AnalyticsTab = "risk" | "rebalance";

const CAPS = [0.25, 0.35, 0.5, 1.0];

/**
 * The Risk & Return and Suggest Rebalance surface.
 *
 * A right-edge drawer rather than a fourth row in the terminal: the middle column is
 * already three tight rows, and a fourth would squeeze the main chart below usefulness. The
 * drawer leaves the watchlist ticking on the left while you read.
 *
 * The selection lives IN here rather than as checkboxes threaded through the positions
 * table and the watchlist. Both buttons open the same drawer on different tabs, so one
 * selection serves both questions - "what is the risk of holding these?" and "what should
 * I hold instead?" - and the dense terminal layout is untouched.
 */
export function AnalyticsPanel({
  open,
  tab,
  positions,
  watchlist,
  onTab,
  onClose,
  onApplied,
}: {
  open: boolean;
  tab: AnalyticsTab;
  positions: LivePosition[];
  watchlist: string[];
  onTab: (tab: AnalyticsTab) => void;
  onClose: () => void;
  onApplied: () => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  // False until the selection has been seeded; gates the first fetch (see below).
  const [ready, setReady] = useState(false);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [objective, setObjective] = useState<RebalanceObjective>("min_variance");
  const [maxWeight, setMaxWeight] = useState(0.35);

  const [risk, setRisk] = useState<RiskStats | null>(null);
  const [plan, setPlan] = useState<RebalancePlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const universe = useMemo(() => {
    const held = positions.map((position) => position.ticker);
    return Array.from(new Set([...held, ...watchlist])).sort();
  }, [positions, watchlist]);

  const liveWeights = useMemo(() => {
    const invested = positions.reduce((sum, p) => sum + p.live_market_value, 0);
    const map: Record<string, number> = {};
    for (const position of positions) {
      map[position.ticker] = invested > 0 ? position.live_market_value / invested : 0;
    }
    return map;
  }, [positions]);

  // Seed the selection from what the user actually holds, once. Re-seeding on every price
  // tick would fight the user's edits mid-session.
  //
  // `ready` exists to order this against the fetch below. Both effects run in the same
  // commit, so without the gate the first request goes out with an empty selection: with
  // positions that is invisible (the backend's own default is the same portfolio), but on an
  // all-cash account it asks for nothing and gets a 400 back.
  useEffect(() => {
    if (!open || ready) return;
    const held = positions.filter((p) => p.live_priced).map((p) => p.ticker);
    const seeded = held.length > 0 ? held : universe.slice(0, 5);
    if (seeded.length === 0) return;          // watchlist not loaded yet; retry on next prop
    setSelected((current) => (current.length > 0 ? current : seeded));
    setWeights((current) => (Object.keys(current).length > 0 ? current : liveWeights));
    setReady(true);
  }, [open, ready, positions, universe, liveWeights]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const holdings = useCallback((): HoldingInput[] => {
    const total = selected.reduce((sum, ticker) => sum + (weights[ticker] ?? 0), 0);
    return selected.map((ticker) => ({
      ticker,
      // No weights at all (a fresh selection of unheld names) means "equally", which the
      // backend applies. Sending explicit zeros would instead ask for an empty portfolio.
      weight: total > 0 ? (weights[ticker] ?? 0) / total : undefined,
    }));
  }, [selected, weights]);

  const run = useCallback(
    async (which: AnalyticsTab) => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        if (which === "risk") {
          setRisk(await api.postRisk(holdings()));
        } else {
          setPlan(await api.postRebalance(objective, holdings(), maxWeight));
        }
      } catch (exception) {
        setError(exception instanceof Error ? exception.message : "Request failed");
        if (which === "risk") setRisk(null);
        else setPlan(null);
      } finally {
        setLoading(false);
      }
    },
    [holdings, objective, maxWeight],
  );

  // Recompute when the tab, the objective or the cap changes - all deliberate actions. Edits
  // to the selection do not auto-fire: retyping a weight would otherwise send a request per
  // keystroke, each one shifting the chart under the cursor.
  useEffect(() => {
    if (open && ready) void run(tab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ready, tab, objective, maxWeight]);

  const toggle = (ticker: string) => {
    setSelected((current) =>
      current.includes(ticker)
        ? current.filter((item) => item !== ticker)
        : [...current, ticker].sort(),
    );
  };

  const handleApply = async () => {
    if (!plan) return;
    setApplying(true);
    setError(null);
    try {
      const result = await api.applyRebalance(plan.trades);
      onApplied();
      // Re-price the suggestion against the book that now exists, so the panel shows the
      // outcome rather than the prediction...
      await run("rebalance");
      // ...and post the confirmation AFTER that, never before: `run` clears the notice on
      // entry, so setting it first wiped the only feedback the user gets that their trades
      // went through - about 100ms after it appeared.
      setNotice(
        result.rejected === 0
          ? `Filled all ${result.filled} trades.`
          : `Filled ${result.filled}, rejected ${result.rejected}. ` +
            (result.trades.find((trade) => trade.status === "rejected")?.reason ?? ""),
      );
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  };

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-base/70"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className="panel fixed inset-y-0 right-0 z-50 flex w-[min(780px,95vw)] flex-col shadow-2xl"
        role="dialog"
        aria-label="Portfolio analytics"
        data-testid="analytics-panel"
      >
        <div className="panel-title">
          <div className="flex items-center gap-1">
            {(["risk", "rebalance"] as const).map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => onTab(id)}
                data-testid={`analytics-tab-${id}`}
                className={`px-2 py-1 text-2xs font-semibold uppercase tracking-[0.14em] transition-colors ${
                  tab === id
                    ? "border-b-2 border-brand text-ink"
                    : "border-b-2 border-transparent text-muted hover:text-ink"
                }`}
              >
                {id === "risk" ? "Risk & return" : "Suggest rebalance"}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="px-2 text-sm text-muted hover:text-ink"
            aria-label="Close analytics"
          >
            &times;
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-3">
          <section className="mb-3 border border-edge bg-raised p-2">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-2xs font-semibold uppercase tracking-wider text-muted">
                Selection · {selected.length} names
              </span>
              <div className="flex gap-1">
                <button
                  type="button"
                  className="btn btn-ghost !py-1"
                  onClick={() => {
                    setSelected(positions.filter((p) => p.live_priced).map((p) => p.ticker));
                    setWeights(liveWeights);
                  }}
                >
                  My positions
                </button>
                <button
                  type="button"
                  className="btn btn-ghost !py-1"
                  onClick={() => void run(tab)}
                  disabled={loading || selected.length === 0}
                  data-testid="analytics-recalculate"
                >
                  {loading ? "Working…" : "Recalculate"}
                </button>
              </div>
            </div>

            <div className="flex flex-wrap gap-1">
              {universe.map((ticker) => {
                const on = selected.includes(ticker);
                return (
                  <button
                    key={ticker}
                    type="button"
                    onClick={() => toggle(ticker)}
                    data-testid={`analytics-pick-${ticker}`}
                    className={`border px-1.5 py-0.5 font-mono text-2xs tracking-wide transition-colors ${
                      on
                        ? "border-brand bg-brand/15 text-ink"
                        : "border-edge text-faint hover:border-edgeBright hover:text-muted"
                    }`}
                  >
                    {ticker}
                    {on && weights[ticker] != null && (
                      <span className="ml-1 text-faint">
                        {(weights[ticker] * 100).toFixed(0)}%
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
            <p className="mt-1 text-2xs text-faint">
              Weights come from what you hold; deselected names are excluded. Press
              Recalculate after changing the selection.
            </p>
          </section>

          {tab === "rebalance" && (
            <section className="mb-3 flex flex-wrap items-center gap-3 border border-edge bg-raised p-2">
              <div className="flex flex-wrap gap-1">
                {OBJECTIVES.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    title={option.blurb}
                    onClick={() => setObjective(option.id)}
                    data-testid={`objective-${option.id}`}
                    className={`border px-2 py-1 text-2xs font-semibold uppercase tracking-wider transition-colors ${
                      objective === option.id
                        ? "border-brand bg-brand/15 text-ink"
                        : "border-edge text-muted hover:border-edgeBright hover:text-ink"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <label className="flex items-center gap-1 text-2xs text-faint">
                Cap
                <select
                  className="field !py-0.5 text-2xs"
                  value={maxWeight}
                  onChange={(event) => setMaxWeight(Number(event.target.value))}
                >
                  {CAPS.map((cap) => (
                    <option key={cap} value={cap}>
                      {cap === 1 ? "none" : `${cap * 100}%`}
                    </option>
                  ))}
                </select>
              </label>
              <p className="basis-full text-2xs text-faint">
                {OBJECTIVES.find((option) => option.id === objective)?.blurb}
              </p>
            </section>
          )}

          {error && (
            <p
              className="mb-3 border border-down/40 bg-down/10 px-2 py-1.5 text-xs text-down"
              data-testid="analytics-error"
            >
              {error}
            </p>
          )}
          {notice && (
            <p className="mb-3 border border-up/40 bg-up/10 px-2 py-1.5 text-xs text-up">
              {notice}
            </p>
          )}

          {loading && !risk && !plan && (
            <p className="py-10 text-center text-xs text-faint">Calculating…</p>
          )}

          {tab === "risk" && risk && <RiskReport stats={risk} />}
          {tab === "rebalance" && plan && (
            <RebalancePreview plan={plan} applying={applying} onApply={() => void handleApply()} />
          )}
        </div>
      </aside>
    </>
  );
}
