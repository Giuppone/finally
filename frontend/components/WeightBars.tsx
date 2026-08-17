"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  AXIS_FONT,
  AXIS_TEXT,
  GRID,
  SERIES_ACTUAL,
  SERIES_MODEL,
  TOOLTIP_STYLE,
  percentTooltip,
} from "@/lib/chart";

export interface WeightRow {
  ticker: string;
  /** Blue series — what the book is today. */
  actual: number;
  /** Amber series — the other measure: risk share, or the proposed target. */
  model: number;
}

const ROW_HEIGHT = 26;
const AXIS_BAND = 42;

/**
 * Two measures per ticker, side by side. Used twice: weight vs risk share on the Risk tab,
 * and current vs target weight on the Rebalance tab.
 *
 * Horizontal, because the category labels are ticker symbols and a vertical layout would
 * either rotate them or truncate them. Height grows with the row count rather than being
 * fixed - a fixed height would squeeze the x-axis band out of the card and produce a tiny
 * nested scrollbar.
 */
export function WeightBars({
  rows,
  actualLabel,
  modelLabel,
  testId,
}: {
  rows: WeightRow[];
  actualLabel: string;
  modelLabel: string;
  testId?: string;
}) {
  if (rows.length === 0) return null;

  return (
    <div style={{ height: rows.length * ROW_HEIGHT + AXIS_BAND }} data-testid={testId}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 4, right: 14, bottom: 2, left: 2 }}
          // 2px of surface between the paired bars; the rest of the band stays as air.
          barGap={2}
          barCategoryGap="24%"
        >
          <CartesianGrid stroke={GRID} horizontal={false} strokeDasharray="" />
          <XAxis
            type="number"
            unit="%"
            tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
            stroke={GRID}
          />
          <YAxis
            type="category"
            dataKey="ticker"
            width={46}
            tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
            stroke={GRID}
          />
          <Tooltip
            {...TOOLTIP_STYLE}
            formatter={percentTooltip}
          />
          <Legend
            verticalAlign="top"
            height={20}
            iconSize={8}
            wrapperStyle={{ fontSize: 10, color: AXIS_TEXT }}
          />
          {/* maxBarSize caps the thickness so a two-row chart does not render two slabs. */}
          <Bar
            name={actualLabel}
            dataKey="actual"
            fill={SERIES_ACTUAL}
            maxBarSize={9}
            radius={[0, 4, 4, 0]}
            isAnimationActive={false}
          />
          <Bar
            name={modelLabel}
            dataKey="model"
            fill={SERIES_MODEL}
            maxBarSize={9}
            radius={[0, 4, 4, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
