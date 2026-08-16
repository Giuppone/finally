"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DASH, clockTime, money, percent, toneClass } from "@/lib/format";
import type { PricePoint, Quote } from "@/lib/types";

function ChartTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as PricePoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">{money(point.price)}</div>
      <div className="tnum text-faint">{clockTime(point.ts)}</div>
    </div>
  );
}

export function MainChart({
  ticker,
  points,
  quote,
}: {
  ticker: string | null;
  points: PricePoint[];
  quote: Quote | undefined;
}) {
  const changePct = quote?.change_pct;
  const stroke = (changePct ?? 0) >= 0 ? "#3fb950" : "#f85149";

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
                open {money(quote.open_price)}
              </span>
            </>
          )}
        </div>
        <span className="tnum normal-case tracking-normal text-faint">
          {points.length > 0 ? `${points.length} pts` : DASH}
        </span>
      </div>

      <div className="min-h-0 flex-1 p-2">
        {points.length < 2 ? (
          <div className="flex h-full items-center justify-center text-xs text-faint">
            {ticker ? "Waiting for price history…" : "Select a ticker from the watchlist"}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="#232b36" strokeDasharray="2 4" vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={clockTime}
                tick={{ fill: "#5b6673", fontSize: 10 }}
                stroke="#232b36"
                minTickGap={48}
              />
              <YAxis
                domain={["dataMin", "dataMax"]}
                tickFormatter={(value: number) => value.toFixed(2)}
                tick={{ fill: "#5b6673", fontSize: 10 }}
                stroke="#232b36"
                width={56}
                orientation="right"
              />
              {/* The session anchor daily change is measured from (PLAN.md §6). */}
              {quote && (
                <ReferenceLine
                  y={quote.open_price}
                  stroke="#5b6673"
                  strokeDasharray="4 4"
                  strokeWidth={1}
                />
              )}
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "#303b48" }} />
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
        )}
      </div>
    </section>
  );
}
