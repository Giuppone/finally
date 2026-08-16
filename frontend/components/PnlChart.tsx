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

import { money, timeOfDay } from "@/lib/format";
import type { SnapshotPoint } from "@/lib/types";

function PnlTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload as SnapshotPoint;
  return (
    <div className="border border-edgeBright bg-base/95 px-2 py-1 text-2xs">
      <div className="tnum text-ink">{money(point.total_value)}</div>
      <div className="tnum text-faint">{timeOfDay(point.recorded_at)}</div>
    </div>
  );
}

export function PnlChart({
  points,
  startingCash,
}: {
  points: SnapshotPoint[];
  startingCash: number;
}) {
  const latest = points[points.length - 1]?.total_value ?? startingCash;
  const ahead = latest >= startingCash;
  const stroke = ahead ? "#3fb950" : "#f85149";

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>Portfolio Value</span>
        <span className="tnum normal-case tracking-normal text-faint">
          {points.length} snapshots
        </span>
      </div>

      <div className="min-h-0 flex-1 p-2" data-testid="pnl-chart">
        {points.length < 2 ? (
          <div className="flex h-full items-center justify-center text-center text-xs text-faint">
            Building history — a snapshot is recorded every 30s and after every trade.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="pnl-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#232b36" strokeDasharray="2 4" vertical={false} />
              <XAxis
                dataKey="recorded_at"
                tickFormatter={timeOfDay}
                tick={{ fill: "#5b6673", fontSize: 10 }}
                stroke="#232b36"
                minTickGap={40}
              />
              <YAxis
                domain={["dataMin", "dataMax"]}
                tickFormatter={(value: number) => value.toFixed(0)}
                tick={{ fill: "#5b6673", fontSize: 10 }}
                stroke="#232b36"
                width={56}
                orientation="right"
              />
              {/* Break-even. The whole chart is "am I above or below $10,000". */}
              <ReferenceLine y={startingCash} stroke="#5b6673" strokeDasharray="4 4" />
              <Tooltip content={<PnlTooltip />} cursor={{ stroke: "#303b48" }} />
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
        )}
      </div>
    </section>
  );
}
