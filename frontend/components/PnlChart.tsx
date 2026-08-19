"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS_FONT, AXIS_TEXT, GRID } from "@/lib/chart";
import { longDate, money, shortDate, signedPercent, timeOfDay } from "@/lib/format";
import type { CurvePoint, PortfolioCurve, SnapshotPoint } from "@/lib/types";

import { BasisToggle, RangeSelector, type ChartRange } from "./RangeSelector";

const UP = "#3fb950";
const DOWN = "#f85149";

/**
 * Two series share this panel, and they are not the same account.
 *
 * LIVE draws `portfolio_snapshots` — the $10,000 paper book, sampled every 30s, break-even at
 * STARTING_CASH. Every other range draws the reconstructed real brokerage book at daily US
 * closes, break-even at whatever it was worth on the first day of the window. They share an
 * axis and a shape language and nothing else, so the header states which one is on screen
 * rather than leaving the reader to infer it from the numbers.
 */

function LiveTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as SnapshotPoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">{money(point.total_value)}</div>
      <div className="tnum text-faint">{timeOfDay(point.recorded_at)}</div>
    </div>
  );
}

function CurveTooltip({
  active,
  payload,
  basis,
}: {
  active?: boolean;
  payload?: any[];
  basis: "value" | "percent";
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as CurvePoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">
        {basis === "percent" ? signedPercent(point.return_pct) : money(point.total_value)}
      </div>
      <div className="tnum text-faint">{longDate(point.ts)}</div>
      {point.carry_value > 0.5 && (
        <div className="tnum text-faint">carried {money(point.carry_value)}</div>
      )}
    </div>
  );
}

/** Shared chrome, so the two modes cannot drift apart visually. */
function fill(stroke: string) {
  return (
    <defs>
      <linearGradient id="pnl-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
        <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
      </linearGradient>
    </defs>
  );
}

const Y_AXIS = {
  domain: ["dataMin", "dataMax"] as [string, string],
  tick: { fill: AXIS_TEXT, fontSize: AXIS_FONT },
  stroke: GRID,
  width: 56,
  orientation: "right" as const,
};

/**
 * The two modes render separately rather than through one chart with a union `data` prop.
 * Recharts infers its point type from that prop, so a `SnapshotPoint[] | CurvePoint[]` union
 * does not type-check — and the modes already differ in axis, tooltip, dataKey and reference
 * line, so sharing the element saved nothing anyway.
 */
function LiveArea({ points, startingCash, stroke }: {
  points: SnapshotPoint[];
  startingCash: number;
  stroke: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        {fill(stroke)}
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="recorded_at"
          tickFormatter={timeOfDay}
          tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
          stroke={GRID}
          minTickGap={40}
        />
        <YAxis {...Y_AXIS} tickFormatter={(value: number) => value.toFixed(0)} />
        {/* Break-even. The live chart is "am I above or below $10,000". */}
        <ReferenceLine y={startingCash} stroke={AXIS_TEXT} strokeDasharray="4 4" />
        <Tooltip content={<LiveTooltip />} cursor={{ stroke: "#303b48" }} />
        <Area
          type="monotone"
          dataKey="total_value"
          stroke={stroke}
          strokeWidth={1.5}
          fill="url(#pnl-fill)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function CurveArea({ points, baseline, basis, stroke }: {
  points: CurvePoint[];
  baseline: number;
  basis: "value" | "percent";
  stroke: string;
}) {
  const percent = basis === "percent";
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        {fill(stroke)}
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="ts"
          type="number"
          domain={["dataMin", "dataMax"]}
          tickFormatter={shortDate}
          tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
          stroke={GRID}
          minTickGap={48}
        />
        <YAxis
          {...Y_AXIS}
          tickFormatter={(value: number) =>
            percent ? `${value.toFixed(0)}%` : value.toFixed(0)
          }
        />
        {/* Break-even is the window's own first close, not $10,000 — this is a different
            account, and measuring it against the paper book's starting cash means nothing. */}
        <ReferenceLine
          y={percent ? 0 : baseline}
          stroke={AXIS_TEXT}
          strokeDasharray="4 4"
        />
        <Tooltip content={<CurveTooltip basis={basis} />} cursor={{ stroke: "#303b48" }} />
        <Area
          type="monotone"
          dataKey={percent ? "return_pct" : "total_value"}
          stroke={stroke}
          strokeWidth={1.5}
          fill="url(#pnl-fill)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function PnlChart({
  points,
  startingCash,
  curve,
  range,
  onRangeChange,
  basis,
  onBasisChange,
  loading = false,
}: {
  points: SnapshotPoint[];
  startingCash: number;
  /** null while the first fetch is in flight; `available: false` on a build with no ledger. */
  curve: PortfolioCurve | null;
  range: ChartRange;
  onRangeChange: (next: ChartRange) => void;
  basis: "value" | "percent";
  onBasisChange: (next: "value" | "percent") => void;
  loading?: boolean;
}) {
  const hasHistory = curve?.available ?? false;
  // Offering a range the backend cannot answer is worse than not offering it: the user picks
  // 6M, gets an empty panel, and has no way to tell a missing ledger from a bug.
  const options: ChartRange[] = hasHistory
    ? ["live", "1m", "3m", "6m", "ytd", "max"]
    : ["live"];

  const isLive = range === "live" || !hasHistory;
  const curvePoints = curve?.points ?? [];

  const latest = isLive
    ? points[points.length - 1]?.total_value ?? startingCash
    : curvePoints[curvePoints.length - 1]?.total_value ?? 0;
  const baseline = isLive ? startingCash : curve?.base_value ?? 0;
  const ahead = latest >= baseline;
  const stroke = ahead ? UP : DOWN;

  const enough = isLive ? points.length >= 2 : curvePoints.length >= 2;

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>Portfolio Value</span>
        <div className="flex items-center gap-1.5">
          {!isLive && (
            <BasisToggle value={basis} onChange={onBasisChange} testId="pnl-basis" />
          )}
          <RangeSelector
            value={range}
            options={options}
            onChange={onRangeChange}
            testIdPrefix="pnl"
            disabled={loading}
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 p-2" data-testid="pnl-chart" data-range={range}>
        {!enough ? (
          <div className="flex h-full items-center justify-center px-4 text-center text-xs text-faint">
            {isLive
              ? "Building history — a snapshot is recorded every 30s and after every trade."
              : "No reconstructed history on this build. Run scripts/import_broker_with_dates."}
          </div>
        ) : isLive ? (
          <LiveArea points={points} startingCash={startingCash} stroke={stroke} />
        ) : (
          <CurveArea
            points={curvePoints}
            baseline={baseline}
            basis={basis}
            stroke={stroke}
          />
        )}
      </div>

      <div className="flex items-center justify-between border-t border-edge px-2 py-1 text-2xs text-faint">
        {isLive ? (
          <span className="tnum">{points.length} snapshots</span>
        ) : (
          <>
            <span className="tnum">
              {money(curve?.base_value ?? 0)} &rarr; {money(latest)}{" "}
              <span className={ahead ? "text-up" : "text-down"}>
                {signedPercent(curvePoints[curvePoints.length - 1]?.return_pct ?? 0, 1)}
              </span>
            </span>
            {/* The curve ends at the last cached daily bar, not today. Saying so beats
                letting a four-day gap read as the book having stopped moving. */}
            <span className="tnum">through {curve?.end_date ?? "—"}</span>
          </>
        )}
      </div>
    </section>
  );
}
