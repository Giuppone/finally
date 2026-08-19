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

// The two font stacks from tailwind.config.ts, restated here because SVG <text> is outside
// Tailwind's reach. The split follows the app-wide convention (see Watchlist, PositionsTable):
// tickers are SANS semibold with a little tracking, numbers are MONO with tabular figures.
// The first version set everything in bold Consolas and scaled it to 30px on big tiles -
// which read less "trading terminal" and more "ransom note".
const SANS = 'ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif';
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

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

  // Type scales with the tile - area carries meaning in a treemap - but gently, and capped
  // low: 19px semibold is already unmistakably "the big position", while the old 30px cap
  // shouted. The floor stays at 10px, below which nothing is worth rendering.
  const span = Math.min(width, height);
  const nameSize = Math.max(10, Math.min(span / 5, 19));
  const pctSize = Math.max(9, nameSize * 0.78);
  const weightSize = Math.max(8.5, nameSize * 0.66);

  // Estimated line widths, so a label is only attempted where it fits. The clip below is the
  // hard guarantee; these gates exist so a tile shows nothing rather than a clipped stump.
  const nameWidth = name.length * nameSize * 0.68;
  const roomForLabel = width > nameWidth + 10 && height > nameSize + 10;
  const roomForPct =
    roomForLabel && width > pctSize * 5.5 && height > nameSize + pctSize * 1.45 + 12;
  const roomForWeight =
    roomForPct && width > weightSize * 6.5 && height > nameSize + (pctSize + weightSize) * 1.45 + 16;

  const centreX = x + width / 2;
  const block =
    nameSize + (roomForPct ? pctSize * 1.45 : 0) + (roomForWeight ? weightSize * 1.45 : 0);
  const top = y + height / 2 - block / 2 + nameSize * 0.8;

  // Tickers are unique within one book, so the name doubles as a stable clip id. The clip is
  // what makes overflow impossible: without it a label on a narrow tile bleeds across the
  // 2px gap into the neighbour and the two smear into an unreadable pile-up.
  const clipId = `hm-clip-${name}`;

  return (
    <g>
      <defs>
        <clipPath id={clipId}>
          <rect x={x + 2} y={y + 2} width={Math.max(0, width - 4)} height={Math.max(0, height - 4)} />
        </clipPath>
      </defs>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={fillFor(pnlPct)}
        stroke="#0d1117"
        strokeWidth={2}
      />
      <g clipPath={`url(#${clipId})`}>
        {roomForLabel && (
          <text
            x={centreX}
            y={top}
            textAnchor="middle"
            fill="#e6edf3"
            fontSize={nameSize}
            fontWeight={600}
            fontFamily={SANS}
            letterSpacing="0.04em"
          >
            {name}
          </text>
        )}
        {roomForLabel && roomForPct && (
          <text
            x={centreX}
            y={top + pctSize * 1.45}
            textAnchor="middle"
            // Light tints, not the saturated #3fb950/#f85149 used elsewhere: those are tuned
            // for the near-black page background and turn to mud on a tile already tinted
            // the same hue. The tile carries the sign; the text only has to stay readable.
            fill={pnlPct >= 0 ? "#9ff0a9" : "#ffb4ad"}
            fontSize={pctSize}
            fontFamily={MONO}
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {priced ? percent(pnlPct) : "—"}
          </text>
        )}
        {roomForLabel && roomForPct && roomForWeight && (
          <text
            x={centreX}
            y={top + pctSize * 1.45 + weightSize * 1.45}
            textAnchor="middle"
            fill="#aab6c2"
            fontSize={weightSize}
            fontFamily={MONO}
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {(weight * 100).toFixed(1)}%
          </text>
        )}
      </g>
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
