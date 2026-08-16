"use client";

import { memo } from "react";
import { Line, LineChart, YAxis } from "recharts";

import type { PricePoint } from "@/lib/types";

/**
 * Fixed-size on purpose: no `ResponsiveContainer`. Each one carries a ResizeObserver, and a
 * watchlist of ten re-rendering twice a second makes that the dominant cost in the grid.
 */
export const Sparkline = memo(
  function Sparkline({
    points,
    width = 76,
    height = 26,
  }: {
    points: PricePoint[];
    width?: number;
    height?: number;
  }) {
    const data = points.length > 60 ? points.slice(-60) : points;

    if (data.length < 2) {
      // Reserve the space so a ticker gaining history does not shift the row.
      return <div style={{ width, height }} aria-hidden />;
    }

    const first = data[0].price;
    const last = data[data.length - 1].price;
    const stroke = last > first ? "#3fb950" : last < first ? "#f85149" : "#8b97a6";

    return (
      <LineChart
        width={width}
        height={height}
        data={data}
        margin={{ top: 3, right: 2, bottom: 3, left: 2 }}
      >
        {/* Without an explicit min/max domain the line is squashed against a 0 baseline and
            every ticker looks flat. */}
        <YAxis hide domain={["dataMin", "dataMax"]} />
        <Line
          type="monotone"
          dataKey="price"
          stroke={stroke}
          strokeWidth={1.25}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    );
  },
  (before, after) =>
    before.points.length === after.points.length &&
    before.points[before.points.length - 1]?.price ===
      after.points[after.points.length - 1]?.price,
);
