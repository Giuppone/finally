"use client";

import { RiskScatter } from "@/components/RiskScatter";
import { WeightBars, type WeightRow } from "@/components/WeightBars";
import { SERIES_ACTUAL, SERIES_MODEL } from "@/lib/chart";
import { DASH, money } from "@/lib/format";
import type { RiskStats } from "@/lib/types";

export function Stat({
  label,
  value,
  hint,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  /** Goes on the value, not the tile — the E2E reads the number, not the label. */
  testId?: string;
}) {
  return (
    <div className="border border-edge bg-raised px-2.5 py-2">
      <div className="text-2xs uppercase tracking-wider text-faint">{label}</div>
      {/* Proportional figures deliberately: tabular-nums makes a standalone value look
          loose at this size. The tables below are where digits need to line up. */}
      <div className="mt-0.5 text-lg font-semibold leading-tight text-ink" data-testid={testId}>
        {value}
      </div>
      {hint && <div className="text-2xs text-faint">{hint}</div>}
    </div>
  );
}

const pct = (value: number | null | undefined, digits = 1) =>
  value == null || !Number.isFinite(value) ? DASH : `${(value * 100).toFixed(digits)}%`;

export function RiskReport({ stats }: { stats: RiskStats }) {
  const rows: WeightRow[] = stats.positions
    .filter((position) => position.weight > 0)
    .map((position) => ({
      ticker: position.ticker,
      actual: position.weight * 100,
      model: position.risk_share * 100,
    }))
    .sort((a, b) => b.model - a.model);

  // The headline the panel exists to deliver: the name carrying the most risk is very
  // often not the name carrying the most money.
  const heaviest = [...stats.positions].sort((a, b) => b.weight - a.weight)[0];
  const riskiest = [...stats.positions].sort((a, b) => b.risk_share - a.risk_share)[0];
  const mismatch = heaviest && riskiest && heaviest.ticker !== riskiest.ticker;

  return (
    <div className="flex flex-col gap-3" data-testid="risk-report">
      <div className="grid grid-cols-3 gap-2">
        <Stat
          label="Volatility"
          value={pct(stats.volatility)}
          hint="annualised"
          testId="risk-volatility"
        />
        <Stat
          label="Expected return"
          value={pct(stats.expected_return)}
          hint="annualised"
          testId="risk-expected-return"
        />
        <Stat
          label="Sharpe"
          value={stats.sharpe == null ? DASH : stats.sharpe.toFixed(2)}
          hint={`vs ${pct(stats.risk_free_rate, 0)} risk-free`}
          testId="risk-sharpe"
        />
        <Stat
          label="1-day VaR 95%"
          value={money(stats.var_95_1d_parametric)}
          hint="parametric"
          testId="risk-var"
        />
        <Stat
          label="Effective names"
          value={stats.effective_n ? stats.effective_n.toFixed(1) : DASH}
          hint={`${pct(stats.cash_weight, 0)} in cash`}
          testId="risk-effective-n"
        />
        <Stat
          label="Diversification"
          value={
            stats.diversification_ratio == null
              ? DASH
              : `${stats.diversification_ratio.toFixed(2)}x`
          }
          hint="1.00 = none"
          testId="risk-diversification"
        />
      </div>

      {/* The expected return above is the simulator's damped drift. Never show it bare. */}
      <p className="border-l-2 border-accent/70 bg-accent/5 px-2 py-1 text-2xs text-muted">
        Expected return basis: {stats.expected_return_basis}. Volatility and correlation are
        the calibrated parameters the price engine itself uses; the drift is damped, so read
        it as the model&apos;s assumption, not a forecast.
      </p>

      {stats.warnings.length > 0 && (
        <ul className="space-y-0.5 text-2xs text-accent">
          {stats.warnings.map((warning) => (
            <li key={warning}>! {warning}</li>
          ))}
        </ul>
      )}

      <section>
        <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">
          Risk / return map
        </h3>
        <RiskScatter stats={stats} />
      </section>

      <section>
        <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">
          Money vs risk
        </h3>
        {mismatch && (
          <p className="mb-1 text-2xs text-muted">
            <span style={{ color: SERIES_ACTUAL }}>&#9632;</span> {heaviest.ticker} holds the
            most money ({pct(heaviest.weight)});{" "}
            <span style={{ color: SERIES_MODEL }}>&#9632;</span> {riskiest.ticker} supplies
            the most risk ({pct(riskiest.risk_share)}).
          </p>
        )}
        <WeightBars
          rows={rows}
          actualLabel="Weight"
          modelLabel="Risk share"
          testId="risk-contributions"
        />
      </section>

      {/* Every charted value is also readable here: a tooltip must never be the only way
          to get at a number. */}
      <section>
        <h3 className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">
          Detail
        </h3>
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-edge text-2xs uppercase tracking-wider text-faint">
              <th className="py-1 pr-2 text-left font-medium">Ticker</th>
              <th className="px-2 text-right font-medium">Weight</th>
              <th className="px-2 text-right font-medium">Vol</th>
              <th className="px-2 text-right font-medium">Risk share</th>
              <th className="pl-2 text-right font-medium">Marginal</th>
            </tr>
          </thead>
          <tbody>
            {stats.positions.map((position) => (
              <tr key={position.ticker} className="border-b border-edge/60 last:border-b-0">
                <td className="py-1 pr-2 font-semibold tracking-wide">
                  {position.ticker}
                  {!position.calibrated && (
                    <span className="ml-1 text-2xs font-normal text-accent" title="Generic parameters, not measured">
                      ~
                    </span>
                  )}
                </td>
                <td className="tnum px-2 text-right text-muted">{pct(position.weight)}</td>
                <td className="tnum px-2 text-right text-muted">
                  {pct(position.volatility, 0)}
                </td>
                <td className="tnum px-2 text-right">{pct(position.risk_share)}</td>
                <td className="tnum pl-2 text-right text-muted">
                  {pct(position.marginal_risk, 0)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
