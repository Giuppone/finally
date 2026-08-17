"use client";

import {
  CartesianGrid,
  LabelList,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import {
  AXIS_FONT,
  AXIS_TEXT,
  GRID,
  SERIES_ACTUAL,
  SERIES_MODEL,
  SURFACE,
  TOOLTIP_STYLE,
  percentTooltip,
} from "@/lib/chart";
import type { RiskStats } from "@/lib/types";

interface Point {
  ticker: string;
  x: number;
  y: number;
  z: number;
  weight: number;
}

/**
 * Where each holding sits in risk/return space, and where the portfolio lands.
 *
 * The portfolio point is the story: diversification pulls it left of the weighted average
 * of its holdings, and seeing it sit inside the cloud rather than on it is the clearest
 * statement of what correlation is doing.
 *
 * Points are NOT coloured by ticker. Ten categorical hues is past the ~7 where adjacent
 * classes blur, and it would spend the whole palette on identity that the direct labels
 * already carry. One hue for holdings, one for the portfolio.
 */
export function RiskScatter({ stats }: { stats: RiskStats }) {
  const holdings: Point[] = stats.positions
    .filter((position) => position.weight > 0)
    .map((position) => ({
      ticker: position.ticker,
      x: position.volatility * 100,
      y: position.expected_return * 100,
      z: Math.max(position.weight, 0.01),
      weight: position.weight,
    }));

  const portfolio: Point[] = [{
    ticker: "PORTFOLIO",
    x: stats.volatility * 100,
    y: stats.expected_return * 100,
    z: 1,
    weight: 1 - stats.cash_weight,
  }];

  if (holdings.length === 0) return null;

  // 12% of the spread as breathing room on each end, so the outermost dot and its label
  // both sit inside the plot rather than against the axis.
  const padded: [(min: number) => number, (max: number) => number] = [
    (min) => Math.max(0, min - Math.abs(min) * 0.12 - 1),
    (max) => max + Math.abs(max) * 0.12 + 1,
  ];

  return (
    <div className="h-56" data-testid="risk-scatter">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 14, right: 18, bottom: 26, left: 4 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="" />
          {/* Padded data domains, not [0, max]. Volatility here runs 57-106% and drift
              2-20%, so anchoring either axis at zero spends most of the plot on empty space
              and squeezes every point into one corner - which is exactly where the labels
              then collide. */}
          <XAxis
            type="number"
            dataKey="x"
            name="Volatility"
            unit="%"
            domain={padded}
            tickFormatter={(value: number) => value.toFixed(0)}
            tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
            stroke={GRID}
            label={{
              value: "annualised volatility %",
              position: "insideBottom",
              offset: -16,
              fill: AXIS_TEXT,
              fontSize: AXIS_FONT,
            }}
          />
          <YAxis
            type="number"
            dataKey="y"
            name="Expected return"
            unit="%"
            domain={padded}
            tickFormatter={(value: number) => value.toFixed(0)}
            width={38}
            tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
            stroke={GRID}
          />
          {/* Area, not radius: perceived size scales with area, and the floor keeps the
              smallest holding above the 8px minimum so it stays hoverable. */}
          <ZAxis type="number" dataKey="z" range={[90, 520]} />
          {/* The ticker has to come from the payload: a scatter point's "label" is its x
              value, so the default tooltip header would read "72" and leave the reader with
              no idea which holding they are hovering - the one thing labels cannot be relied
              on for when two dots sit on top of each other. */}
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={{ strokeDasharray: "3 3", stroke: GRID }}
            formatter={percentTooltip}
            labelFormatter={(_label, payload) =>
              (payload?.[0]?.payload as Point | undefined)?.ticker ?? ""
            }
          />
          <Legend
            verticalAlign="top"
            height={22}
            iconSize={8}
            wrapperStyle={{ fontSize: 10, color: AXIS_TEXT }}
          />
          <Scatter
            name="Holding"
            data={holdings}
            fill={SERIES_ACTUAL}
            fillOpacity={0.75}
            stroke={SURFACE}
            strokeWidth={2}
            isAnimationActive={false}
          >
            <LabelList
              dataKey="ticker"
              position="top"
              offset={8}
              fill="#8b97a6"
              fontSize={9}
            />
          </Scatter>
          <Scatter
            name="Portfolio"
            data={portfolio}
            fill={SERIES_MODEL}
            stroke={SURFACE}
            strokeWidth={2}
            shape="diamond"
            isAnimationActive={false}
          />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}
