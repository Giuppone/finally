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
  FRONTIER,
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
}

/**
 * Where each holding sits in risk/return space, where the portfolio lands, and how far
 * inside the efficient frontier that is.
 *
 * The frontier is the answer to "how good is this book?" — every point on it is the most
 * return achievable at that risk, so the vertical gap between the portfolio marker and the
 * curve is exactly the return being left on the table. Without it the scatter shows where
 * you are with nothing to be relative to.
 *
 * Points are NOT coloured by ticker. Ten categorical hues is past the ~7 where adjacent
 * classes blur, and it would spend the whole palette on identity the direct labels already
 * carry. One hue for holdings, one for the portfolio, and a recessive neutral for the
 * frontier — which is a reference curve, not a third series competing for attention.
 */
export function RiskScatter({ stats }: { stats: RiskStats }) {
  const holdings: Point[] = stats.positions
    .filter((position) => position.weight > 0)
    .map((position) => ({
      ticker: position.ticker,
      x: position.volatility * 100,
      y: position.expected_return * 100,
      z: Math.max(position.weight, 0.01),
    }));

  const portfolio: Point[] = [{
    ticker: "PORTFOLIO",
    x: stats.volatility * 100,
    y: stats.expected_return * 100,
    z: 1,
  }];

  const frontier: Point[] = stats.frontier.map((point, index) => ({
    ticker: `frontier-${index}`,
    x: point.volatility * 100,
    y: point.expected_return * 100,
    z: 1,
  }));

  if (holdings.length === 0) return null;

  // 12% of the spread as breathing room on each end, so the outermost dot and its label both
  // sit inside the plot rather than against the axis.
  const padded: [(min: number) => number, (max: number) => number] = [
    (min) => Math.max(0, min - Math.abs(min) * 0.12 - 1),
    (max) => max + Math.abs(max) * 0.12 + 1,
  ];

  return (
    <div className="h-64" data-testid="risk-scatter">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 14, right: 18, bottom: 30, left: 10 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="" />
          {/* Padded data domains, not [0, max]. Volatility here runs 57-106% and drift
              2-20%, so anchoring either axis at zero spends most of the plot on empty space
              and squeezes every point into one corner. */}
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
              value: "Risk  —  annualised volatility (%)",
              position: "insideBottom",
              offset: -18,
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
            width={46}
            tick={{ fill: AXIS_TEXT, fontSize: AXIS_FONT }}
            stroke={GRID}
            label={{
              value: "Expected return (%)",
              angle: -90,
              position: "insideLeft",
              offset: 14,
              style: { textAnchor: "middle" },
              fill: AXIS_TEXT,
              fontSize: AXIS_FONT,
            }}
          />
          {/* Area, not radius: perceived size scales with area, and the floor keeps the
              smallest holding above the 8px minimum so it stays hoverable. */}
          <ZAxis type="number" dataKey="z" range={[90, 520]} />
          {/* The ticker has to come from the payload: a scatter point's "label" is its x
              value, so the default tooltip header would read "72" and leave the reader with
              no idea which holding they are hovering. */}
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={{ strokeDasharray: "3 3", stroke: GRID }}
            formatter={percentTooltip}
            labelFormatter={(_label, payload) => {
              const ticker = (payload?.[0]?.payload as Point | undefined)?.ticker ?? "";
              return ticker.startsWith("frontier-") ? "Efficient frontier" : ticker;
            }}
          />
          <Legend
            verticalAlign="top"
            height={22}
            iconSize={8}
            wrapperStyle={{ fontSize: 10, color: AXIS_TEXT }}
          />

          {/* Drawn FIRST so the markers sit on top of it. `line` connects the points and
              the empty shape suppresses the 40 dots that would otherwise read as a series
              of their own. */}
          {frontier.length > 1 && (
            <Scatter
              name="Efficient frontier"
              data={frontier}
              fill={FRONTIER}
              line={{ stroke: FRONTIER, strokeWidth: 2 }}
              lineJointType="monotoneX"
              shape={() => <g />}
              legendType="line"
              isAnimationActive={false}
            />
          )}

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
            name="Your portfolio"
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
