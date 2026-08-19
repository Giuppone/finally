"use client";

import {
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS_FONT, AXIS_TEXT, GRID } from "@/lib/chart";
import {
  DASH,
  clockTime,
  longDate,
  money,
  percent,
  quantity,
  shortDate,
  toneClass,
} from "@/lib/format";
import type { DailyPoint, DailySeries, PricePoint, Quote, TradeMark } from "@/lib/types";

import { RangeSelector, type ChartRange } from "./RangeSelector";

const UP = "#3fb950";
const DOWN = "#f85149";

function LiveTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as PricePoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">{money(point.price)}</div>
      <div className="tnum text-faint">{clockTime(point.ts)}</div>
    </div>
  );
}

function DailyTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  // A hovered trade marker arrives through the same tooltip as the price line; a `side`
  // field is what tells them apart.
  const trade = payload
    .map((entry) => entry.payload)
    .find((item) => item && typeof item.side === "string") as TradeMark | undefined;
  if (trade) {
    const bought = trade.side === "buy";
    return (
      <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
        <div className={`font-semibold ${bought ? "text-up" : "text-down"}`}>
          {bought ? "BUY" : "SELL"} {quantity(trade.shares)} @ {money(trade.price)}
        </div>
        <div className="tnum text-faint">{longDate(trade.ts)}</div>
        <div className="tnum text-faint">{money(trade.usd)} total</div>
      </div>
    );
  }
  const point = payload[0].payload as DailyPoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">{money(point.close)}</div>
      <div className="tnum text-faint">{longDate(point.ts)}</div>
    </div>
  );
}

/**
 * A buy/sell marker on the daily chart. Without these the chart draws the ticker's market
 * price for the whole range, and a recently-opened position reads as one held all along —
 * the exact misreading that prompted them. The marker sits at the user's actual converted
 * fill price, so its distance from the close line is real information.
 */
function TradeDot(props: any) {
  const { cx, cy, payload } = props;
  if (cx == null || cy == null || !payload) return null;
  const bought = payload.side === "buy";
  return (
    <g data-testid={`trade-marker-${payload.side}`}>
      <circle cx={cx} cy={cy} r={4.5} fill={bought ? UP : DOWN} stroke="#0d1117" strokeWidth={1.5} />
      <text
        x={cx}
        y={cy + 2.6}
        textAnchor="middle"
        fontSize={6.5}
        fontWeight={700}
        fill="#0d1117"
      >
        {bought ? "B" : "S"}
      </text>
    </g>
  );
}

const Y_AXIS = {
  domain: ["dataMin", "dataMax"] as [string, string],
  tickFormatter: (value: number) => value.toFixed(2),
  tick: { fill: AXIS_TEXT, fontSize: AXIS_FONT },
  stroke: GRID,
  width: 56,
  orientation: "right" as const,
};

const GRID_LINES = (
  <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
);

/**
 * The two modes render separately rather than through one chart with a union `data` prop:
 * Recharts infers its point type from that prop, so `PricePoint[] | DailyPoint[]` does not
 * type-check. They already differ in dataKey, tick formatter, tooltip and reference line.
 */
function LiveLine({ points, quote, stroke }: {
  points: PricePoint[];
  quote: Quote | undefined;
  stroke: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        {GRID_LINES}
        <XAxis
          dataKey="ts"
          type="number"
          domain={["dataMin", "dataMax"]}
          tickFormatter={clockTime}
          tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
          stroke={GRID}
          minTickGap={48}
        />
        <YAxis {...Y_AXIS} />
        {/* The session anchor daily change is measured from (PLAN.md §6). It belongs to the
            live view only: on a six-month daily chart today's open references nothing. */}
        {quote && (
          <ReferenceLine
            y={quote.open_price}
            stroke={AXIS_TEXT}
            strokeDasharray="4 4"
            strokeWidth={1}
          />
        )}
        <Tooltip content={<LiveTooltip />} cursor={{ stroke: "#303b48" }} />
        <Line
          type="monotone"
          dataKey="price"
          stroke={stroke}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

function DailyLine({
  points,
  trades,
  stroke,
}: {
  points: DailyPoint[];
  trades: TradeMark[];
  stroke: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
        {GRID_LINES}
        <XAxis
          dataKey="ts"
          type="number"
          domain={["dataMin", "dataMax"]}
          tickFormatter={shortDate}
          tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
          stroke={GRID}
          minTickGap={48}
        />
        <YAxis {...Y_AXIS} />
        <Tooltip content={<DailyTooltip />} cursor={{ stroke: "#303b48" }} />
        <Line
          type="monotone"
          dataKey="close"
          stroke={stroke}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
        {trades.length > 0 && (
          <Scatter
            data={trades}
            dataKey="price"
            shape={<TradeDot />}
            isAnimationActive={false}
          />
        )}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function MainChart({
  ticker,
  points,
  quote,
  daily,
  range,
  onRangeChange,
  loading = false,
}: {
  ticker: string | null;
  points: PricePoint[];
  quote: Quote | undefined;
  /** Daily closes for the selected ticker; null while fetching or when it has none. */
  daily: DailySeries | null;
  range: ChartRange;
  onRangeChange: (next: ChartRange) => void;
  loading?: boolean;
}) {
  const changePct = quote?.change_pct;
  const hasDaily = (daily?.points.length ?? 0) >= 2;
  const isLive = range === "live" || !hasDaily;

  // Only offer the daily ranges once we know this ticker has bars. A name added to the
  // watchlist today streams fine and has no history at all — showing it a MAX button that
  // returns nothing would read as breakage.
  const options: ChartRange[] = hasDaily
    ? ["live", "1m", "3m", "6m", "ytd", "max"]
    : ["live"];

  const dailyPoints = daily?.points ?? [];
  const trades = daily?.trades ?? [];
  const first = dailyPoints[0]?.close ?? 0;
  const last = dailyPoints[dailyPoints.length - 1]?.close ?? 0;
  const stroke = isLive
    ? (changePct ?? 0) >= 0
      ? UP
      : DOWN
    : last >= first
      ? UP
      : DOWN;

  const enough = isLive ? points.length >= 2 : dailyPoints.length >= 2;

  return (
    <section className="panel flex min-h-0 flex-1 flex-col">
      <div className="panel-title">
        <div className="flex items-baseline gap-3">
          <span className="text-xs font-bold tracking-wide text-ink">
            {ticker ?? "No ticker selected"}
          </span>
          {quote && (
            <>
              <span className="tnum text-xs text-ink">{money(quote.price)}</span>
              <span className={`tnum text-xs ${toneClass(changePct)}`}>
                {percent(changePct)}
              </span>
              <span className="tnum text-2xs normal-case tracking-normal text-faint">
                {isLive
                  ? `open ${money(quote.open_price)}`
                  : `${money(first)} → ${money(last)}`}
              </span>
              {/* This is the market's price history, not the holding period — say when the
                  user actually got in, or the chart implies they held it all along. */}
              {!isLive && daily?.held_since && (
                <span
                  className="tnum text-2xs normal-case tracking-normal text-faint"
                  data-testid="held-since"
                >
                  held since {shortDate(Date.parse(daily.held_since))}
                </span>
              )}
            </>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <span className="tnum normal-case tracking-normal text-faint">
            {enough ? `${isLive ? points.length : dailyPoints.length} pts` : DASH}
          </span>
          {ticker && (
            <RangeSelector
              value={range}
              options={options}
              onChange={onRangeChange}
              testIdPrefix="chart"
              disabled={loading}
            />
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 p-2" data-testid="main-chart" data-range={range}>
        {!enough ? (
          <div className="flex h-full items-center justify-center text-xs text-faint">
            {ticker ? "Waiting for price history…" : "Select a ticker from the watchlist"}
          </div>
        ) : isLive ? (
          <LiveLine points={points} quote={quote} stroke={stroke} />
        ) : (
          <DailyLine points={dailyPoints} trades={trades} stroke={stroke} />
        )}
      </div>
    </section>
  );
}
