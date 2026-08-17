"use client";

import { Stat } from "@/components/RiskReport";
import { WeightBars, type WeightRow } from "@/components/WeightBars";
import { DASH, money, quantity, signedMoney } from "@/lib/format";
import type { RebalanceObjective, RebalancePlan } from "@/lib/types";

export const OBJECTIVES: { id: RebalanceObjective; label: string; blurb: string }[] = [
  {
    id: "min_variance",
    label: "Min variance",
    blurb: "Lowest achievable volatility. Uses only the covariance — no return forecast.",
  },
  {
    id: "risk_parity",
    label: "Risk parity",
    blurb: "Every name supplies an equal share of the risk. Also forecast-free.",
  },
  {
    id: "equal_weight",
    label: "Equal weight",
    blurb: "1/n. The baseline worth beating, and the way back from a lopsided book.",
  },
  {
    id: "max_sharpe",
    label: "Max Sharpe",
    blurb: "Best return per unit of risk — the only objective that trusts the damped drift.",
  },
];

const pct = (value: number | null | undefined, digits = 1) =>
  value == null || !Number.isFinite(value) ? DASH : `${(value * 100).toFixed(digits)}%`;

function Delta({ before, after }: { before: number | null; after: number }) {
  if (before == null) return <span className="text-muted">{pct(after)}</span>;
  const change = after - before;
  const better = change <= 0;
  return (
    <span>
      <span className="text-muted">{pct(before)}</span>
      <span className="mx-1 text-faint">&rarr;</span>
      <span className={better ? "text-up" : "text-down"}>{pct(after)}</span>
    </span>
  );
}

export function RebalancePreview({
  plan,
  applying,
  onApply,
}: {
  plan: RebalancePlan;
  applying: boolean;
  onApply: () => void;
}) {
  const rows: WeightRow[] = plan.targets
    .filter((target) => target.current_weight > 0.0005 || target.target_weight > 0.0005)
    .map((target) => ({
      ticker: target.ticker,
      actual: target.current_weight * 100,
      model: target.target_weight * 100,
    }))
    .sort((a, b) => b.model - a.model || b.actual - a.actual);

  const before = plan.before;

  return (
    <div className="flex flex-col gap-3" data-testid="rebalance-preview">
      <div className="grid grid-cols-3 gap-2">
        <Stat
          label="Volatility"
          value={pct(plan.after.volatility)}
          hint={before ? `from ${pct(before.volatility)}` : undefined}
          testId="after-volatility"
        />
        <Stat
          label="Sharpe"
          value={plan.after.sharpe == null ? DASH : plan.after.sharpe.toFixed(2)}
          hint={
            before?.sharpe == null ? undefined : `from ${before.sharpe.toFixed(2)}`
          }
          testId="after-sharpe"
        />
        <Stat
          label="Effective names"
          value={plan.after.effective_n ? plan.after.effective_n.toFixed(1) : DASH}
          hint={before ? `from ${before.effective_n.toFixed(1)}` : undefined}
          testId="after-effective-n"
        />
      </div>

      <p className="text-2xs text-muted">
        Volatility <Delta before={before?.volatility ?? null} after={plan.after.volatility} />
        {" · "}cash after {money(plan.estimated_cash_after)}
        {" · "}cap {pct(plan.constraints.max_weight, 0)} per name
      </p>

      {plan.warnings.length > 0 && (
        <ul className="space-y-0.5 text-2xs text-accent">
          {plan.warnings.map((warning) => (
            <li key={warning}>! {warning}</li>
          ))}
        </ul>
      )}

      <section>
        <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">
          Current vs target weight
        </h3>
        <WeightBars
          rows={rows}
          actualLabel="Current"
          modelLabel="Target"
          testId="rebalance-weights"
        />
      </section>

      <section>
        <div className="mb-1 flex items-baseline justify-between">
          <h3 className="text-2xs font-semibold uppercase tracking-wider text-muted">
            Trades
          </h3>
          <span className="text-2xs text-faint">
            sells first, then buys — the order they execute in
          </span>
        </div>

        {plan.trades.length === 0 ? (
          <p className="border border-edge bg-raised px-3 py-4 text-center text-xs text-faint">
            Already there. Every leg came out under the $10 minimum, so there is nothing
            worth trading.
          </p>
        ) : (
          <table className="w-full border-collapse text-xs" data-testid="rebalance-trades">
            <thead>
              <tr className="border-b border-edge text-2xs uppercase tracking-wider text-faint">
                <th className="py-1 pr-2 text-left font-medium">#</th>
                <th className="px-2 text-left font-medium">Side</th>
                <th className="px-2 text-left font-medium">Ticker</th>
                <th className="px-2 text-right font-medium">Qty</th>
                <th className="px-2 text-right font-medium">Price</th>
                <th className="pl-2 text-right font-medium">Notional</th>
              </tr>
            </thead>
            <tbody>
              {plan.trades.map((trade, index) => (
                <tr
                  key={`${trade.ticker}-${trade.side}-${index}`}
                  className="border-b border-edge/60 last:border-b-0"
                >
                  <td className="py-1 pr-2 text-faint">{index + 1}</td>
                  <td
                    className={`px-2 font-semibold uppercase ${
                      trade.side === "buy" ? "text-up" : "text-down"
                    }`}
                  >
                    {trade.side}
                  </td>
                  <td className="px-2 font-semibold tracking-wide">
                    {trade.ticker}
                    {trade.clamped && (
                      <span
                        className="ml-1 text-2xs font-normal text-accent"
                        title="Shrunk to the cash actually available"
                      >
                        clamped
                      </span>
                    )}
                  </td>
                  <td className="tnum px-2 text-right text-muted">
                    {quantity(trade.quantity)}
                  </td>
                  <td className="tnum px-2 text-right text-muted">{money(trade.price)}</td>
                  <td className="tnum pl-2 text-right">
                    {signedMoney(trade.side === "buy" ? -trade.notional : trade.notional)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <div className="flex items-center justify-between gap-3 border-t border-edge pt-3">
        <p className="text-2xs text-faint">
          Nothing has traded yet. Apply executes all {plan.trades.length} legs in order,
          under one lock.
        </p>
        <button
          type="button"
          className="btn btn-submit shrink-0"
          disabled={applying || plan.trades.length === 0}
          onClick={onApply}
          data-testid="apply-rebalance"
        >
          {applying ? "Applying…" : `Apply ${plan.trades.length} trades`}
        </button>
      </div>
    </div>
  );
}
