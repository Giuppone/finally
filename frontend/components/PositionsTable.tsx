"use client";

import type { LivePosition } from "@/lib/derive";
import { DASH, money, percent, quantity, signedMoney, toneClass } from "@/lib/format";

export function PositionsTable({
  positions,
  selected,
  onSelect,
}: {
  positions: LivePosition[];
  selected: string | null;
  onSelect: (ticker: string) => void;
}) {
  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>Positions</span>
        <span className="tnum text-faint">{positions.length}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-xs" data-testid="positions-table">
          <thead className="sticky top-0 bg-panel">
            <tr className="border-b border-edge text-2xs uppercase tracking-wider text-faint">
              <th className="py-1.5 pl-3 pr-2 text-left font-medium">Ticker</th>
              <th className="px-2 text-right font-medium">Qty</th>
              <th className="px-2 text-right font-medium">Avg Cost</th>
              <th className="px-2 text-right font-medium">Price</th>
              <th className="px-2 text-right font-medium">Value</th>
              <th className="px-2 text-right font-medium">P&amp;L</th>
              <th className="px-3 text-right font-medium">%</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => (
              <tr
                key={position.ticker}
                onClick={() => onSelect(position.ticker)}
                data-testid={`position-row-${position.ticker}`}
                className={`cursor-pointer border-b border-edge/60 last:border-b-0 ${
                  selected === position.ticker ? "bg-brand/10" : "hover:bg-raised"
                }`}
              >
                <td className="py-1.5 pl-3 pr-2 font-semibold tracking-wide">
                  {position.ticker}
                </td>
                <td className="tnum px-2 text-right text-muted">
                  {quantity(position.quantity)}
                </td>
                <td className="tnum px-2 text-right text-muted">
                  {money(position.avg_cost)}
                </td>
                <td className="tnum px-2 text-right">
                  {position.live_priced ? money(position.live_price) : DASH}
                </td>
                <td className="tnum px-2 text-right">
                  {position.live_priced ? money(position.live_market_value) : DASH}
                </td>
                <td
                  className={`tnum px-2 text-right ${toneClass(position.live_unrealized_pnl)}`}
                >
                  {position.live_priced ? signedMoney(position.live_unrealized_pnl) : DASH}
                </td>
                <td
                  className={`tnum px-3 text-right ${toneClass(position.live_unrealized_pnl_pct)}`}
                >
                  {position.live_priced ? percent(position.live_unrealized_pnl_pct) : DASH}
                </td>
              </tr>
            ))}
            {positions.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-faint">
                  No open positions. Use the trade bar below or ask the assistant.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
