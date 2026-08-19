// Chart palette for the analytics panel.
//
// Two categorical slots, one meaning each, held constant across every chart in the panel
// so the reader learns the code once:
//
//   SERIES_ACTUAL  - what the book IS today (current weight)
//   SERIES_MODEL   - the other measure (risk share, or the proposed target)
//
// AMBER IS NOT PLAN.md's #ecad0a. That yellow sits at OKLCH L 0.786, outside the 0.48-0.67
// band a categorical hue needs on a dark surface - it glares against #12181f and flattens
// next to the blue. #b8860b is the same hue snapped to a passing step. The pair validates
// clean on the panel surface: CVD separation dE 23.6 protan / 21.9 tritan against a
// threshold of 8, and both clear 3:1 contrast. #ecad0a stays the UI accent everywhere else.
//
// Colour never carries identity alone here: every chart with two series ships a legend, the
// scatter direct-labels each point with its symbol, and both tabs repeat the numbers in a
// table.

export const SERIES_ACTUAL = "#209dd7";
export const SERIES_MODEL = "#b8860b";

/**
 * The efficient frontier curve.
 *
 * Deliberately NOT a third categorical hue. The frontier is a reference — the boundary the
 * marks are read against — so it stays a muted neutral and lets the two data colours keep
 * carrying identity. A third saturated hue here would make the curve compete with the
 * portfolio marker it exists to measure.
 */
export const FRONTIER = "#8b97a6";

/** Tailwind `edge`. Hairline, solid, one step off the surface - never dashed. */
export const GRID = "#232b36";
/** Tailwind `faint`, for axis ticks. Text tokens only; text never wears a series colour. */
export const AXIS_TEXT = "#5b6673";
/** Tailwind `panel`. The surface gap and the ring around overlapping dots. */
export const SURFACE = "#12181f";

export const AXIS_FONT = 10;

/** Shared Recharts tooltip chrome, so all three charts read as one system. */
export const TOOLTIP_STYLE = {
  contentStyle: {
    background: "#161d26",
    border: "1px solid #303b48",
    borderRadius: 0,
    fontSize: 11,
    padding: "6px 8px",
  },
  labelStyle: { color: "#e6edf3", fontWeight: 600, marginBottom: 2 },
  itemStyle: { color: "#8b97a6", padding: 0 },
  cursor: { fill: "rgba(48, 59, 72, 0.35)" },
} as const;

/**
 * Percent formatter for Recharts tooltips.
 *
 * Parameters are `unknown` because Recharts types the value as `ValueType | undefined` — a
 * narrower `(value: number)` is not assignable to that, and widening here is safer than
 * casting the callback.
 */
export function percentTooltip(value: unknown, name: unknown): [string, string] {
  const text =
    typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "—";
  return [text, String(name ?? "")];
}
