// Display formatting. Every "—" in the UI comes from here: an unpriced value must never
// render as $0.00, which reads as a -100% day (backend/app/routes.py, Review.md B2).

export const DASH = "—";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdCompact = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

export function money(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? DASH : usd.format(value);
}

export function moneyCompact(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? DASH : usdCompact.format(value);
}

/** Signed, two decimals, with the % sign — for daily change and P&L percentages. */
export function percent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function signedMoney(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${usd.format(value)}`;
}

/** Trailing zeros trimmed: 10, 2.5, 0.001 — never 10.000000. */
export function quantity(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

export function clockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Axis tick for a daily series spanning weeks or months.
 *
 * clockTime and timeOfDay both print time of day only, which is right for a chart covering
 * eight minutes and useless for one covering eight months — every tick would read the same.
 * Takes epoch ms so it drops into the same numeric XAxis the intraday charts use.
 */
export function shortDate(ms: number): string {
  return new Date(ms).toLocaleDateString("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
  });
}

/** Long form for tooltips, where there is room for the year. */
export function longDate(ms: number): string {
  return new Date(ms).toLocaleDateString("en-US", {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Signed percent, for the evolution chart's % mode. */
export function signedPercent(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** Tailwind text colour for a signed number. Flat is deliberately muted, not green. */
export function toneClass(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return "text-muted";
  return value > 0 ? "text-up" : "text-down";
}
