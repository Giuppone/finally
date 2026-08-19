"use client";

import type { HistoryRange } from "@/lib/types";

/**
 * "live" is not a history range — it is the streaming view the app has always had.
 *
 * Keeping it in the same control is the point: the user is choosing one thing, how far back
 * to look, and the shortest answer happens to be served by a different source (the SSE ring
 * buffer) than the longer ones (daily closes). Splitting that into two controls would make
 * them reason about the plumbing.
 */
export type ChartRange = "live" | HistoryRange;

const LABELS: Record<ChartRange, string> = {
  live: "LIVE",
  "1m": "1M",
  "3m": "3M",
  "6m": "6M",
  ytd: "YTD",
  max: "MAX",
};

/**
 * Segmented control, sharing the objective-picker treatment in AnalyticsPanel so the two
 * surfaces read as one system.
 */
export function RangeSelector({
  value,
  options,
  onChange,
  testIdPrefix,
  disabled = false,
}: {
  value: ChartRange;
  options: readonly ChartRange[];
  onChange: (next: ChartRange) => void;
  testIdPrefix: string;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-0.5" role="group" aria-label="Time range">
      {options.map((option) => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            disabled={disabled}
            onClick={() => onChange(option)}
            aria-pressed={active}
            data-testid={`${testIdPrefix}-range-${option}`}
            className={`border px-1.5 py-0.5 text-2xs tracking-wide transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              active
                ? "border-brand bg-brand/15 text-ink"
                : "border-edge text-muted hover:border-edgeBright hover:text-ink"
            }`}
          >
            {LABELS[option]}
          </button>
        );
      })}
    </div>
  );
}

/** $ / % toggle. Percent is scale-free, so the two views agree on shape and differ on axis. */
export function BasisToggle({
  value,
  onChange,
  testId,
}: {
  value: "value" | "percent";
  onChange: (next: "value" | "percent") => void;
  testId: string;
}) {
  return (
    <div className="flex items-center gap-0.5" role="group" aria-label="Value basis">
      {(["value", "percent"] as const).map((option) => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            onClick={() => onChange(option)}
            aria-pressed={active}
            data-testid={`${testId}-${option}`}
            className={`border px-1.5 py-0.5 text-2xs transition-colors ${
              active
                ? "border-brand bg-brand/15 text-ink"
                : "border-edge text-muted hover:border-edgeBright hover:text-ink"
            }`}
          >
            {option === "value" ? "$" : "%"}
          </button>
        );
      })}
    </div>
  );
}
