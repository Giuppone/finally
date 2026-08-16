"use client";

import { ResponsiveContainer, Treemap } from "recharts";

import type { LivePosition } from "@/lib/derive";
import { money, percent } from "@/lib/format";

/**
 * P&L percent -> fill. The scale saturates at ±5%, which is the range a session actually
 * produces; mapping to the true min/max instead would repaint the whole heatmap every time
 * one position moved, and a "deep green" would mean something different every second.
 */
function fillFor(pnlPct: number): string {
  const magnitude = Math.min(Math.abs(pnlPct) / 5, 1);
  const alpha = 0.18 + magnitude * 0.62;
  if (pnlPct > 0.001) return `rgba(63, 185, 80, ${alpha})`;
  if (pnlPct < -0.001) return `rgba(248, 81, 73, ${alpha})`;
  return "rgba(139, 151, 166, 0.18)";
}

interface CellDatum {
  name: string;
  size: number;
  pnl: number;
  pnlPct: number;
  weight: number;
  priced: boolean;
  // Recharts' `TreemapDataType` is an open record, so a closed interface is not assignable
  // to it however well the named fields line up.
  [key: string]: string | number | boolean;
}

// Recharts spreads the node's datum into the content element's props, alongside the layout
// geometry it computed. The types there are loose, hence the explicit shape.
function Cell(props: any) {
  const { x, y, width, height, name, pnl, pnlPct, weight, priced } = props as CellDatum & {
    x: number;
    y: number;
    width: number;
    height: number;
  };

  if (!name || width <= 0 || height <= 0) return null;

  // Type scales with the tile. A treemap's whole point is that area carries meaning, so a
  // position holding 80% of the book should read at a glance rather than wear the same
  // 11px corner label as a 2% sliver — and that same label must still fit the sliver.
  const span = Math.min(width, height);
  const nameSize = Math.max(10, Math.min(span / 4.2, 30));
  const pctSize = nameSize * 0.72;
  const weightSize = nameSize * 0.6;

  // Gated on height above all: a wide, short tile has room for the symbol and nothing else,
  // and three lines crammed into 40px is worse than one line with a tooltip.
  const roomForLabel = width > 40 && height > 22;
  // The absolute floor matters as much as the ratio: on a short tile `nameSize` has already
  // collapsed to its 10px minimum, so a purely proportional test stays true and stacks two
  // lines into 40px of height.
  const roomForPct = width > nameSize * 3.6 && height > Math.max(46, nameSize * 2.8);
  const roomForWeight = width > nameSize * 4.5 && height > nameSize * 4.2;
  const roomForSuffix = width > nameSize * 7;

  const centreX = x + width / 2;
  const block =
    nameSize + (roomForPct ? pctSize * 1.5 : 0) + (roomForWeight ? weightSize * 1.5 : 0);
  const top = y + height / 2 - block / 2 + nameSize * 0.82;

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={fillFor(pnlPct)}
        stroke="#0d1117"
        strokeWidth={2}
      />
      {roomForLabel && (
        <text
          x={centreX}
          y={top}
          textAnchor="middle"
          fill="#e6edf3"
          fontSize={nameSize}
          fontWeight={600}
          fontFamily="ui-monospace, Consolas, monospace"
        >
          {name}
        </text>
      )}
      {roomForLabel && roomForPct && (
        <text
          x={centreX}
          y={top + pctSize * 1.5}
          textAnchor="middle"
          // Light tints, not the saturated #3fb950/#f85149 used elsewhere: those are tuned
          // for the near-black page background and turn to mud on a tile already tinted the
          // same hue. The tile carries the sign; the text only has to stay readable.
          fill={pnlPct >= 0 ? "#9ff0a9" : "#ffb4ad"}
          fontSize={pctSize}
          fontFamily="ui-monospace, Consolas, monospace"
        >
          {priced ? percent(pnlPct) : "—"}
        </text>
      )}
      {roomForLabel && roomForPct && roomForWeight && (
        <text
          x={centreX}
          y={top + pctSize * 1.5 + weightSize * 1.5}
          textAnchor="middle"
          fill="#cdd7e2"
          fontSize={weightSize}
          fontFamily="ui-monospace, Consolas, monospace"
        >
          {(weight * 100).toFixed(1)}%{roomForSuffix ? " of book" : ""}
        </text>
      )}
      <title>
        {`${name}  ${money(props.size)}  ${percent(pnlPct)}  P&L ${money(pnl)}`}
      </title>
    </g>
  );
}

export function Heatmap({ positions }: { positions: LivePosition[] }) {
  const data: CellDatum[] = positions
    .filter((position) => position.live_market_value > 0)
    .map((position) => ({
      name: position.ticker,
      size: position.live_market_value,
      pnl: position.live_unrealized_pnl,
      pnlPct: position.live_unrealized_pnl_pct,
      weight: position.live_weight,
      priced: position.live_priced,
    }));

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>Portfolio Heatmap</span>
        <span className="normal-case tracking-normal text-faint">size = weight · colour = P&amp;L</span>
      </div>

      <div className="min-h-0 flex-1 p-1.5" data-testid="heatmap">
        {data.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-faint">
            No positions yet
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <Treemap
              data={data}
              dataKey="size"
              stroke="#0d1117"
              isAnimationActive={false}
              content={<Cell />}
            />
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}
