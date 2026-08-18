# Review: working-tree changes since `HEAD`

## Findings

### P2 — Do not leave a real brokerage export available to commit

[`suggested/sugested.txt`](../suggested/sugested.txt) contains a real account's individual
positions, quantities, prices, cost bases, P&L, and ARS market values. The repository's
`.gitignore` does not exclude it (or equivalent local broker exports), so a normal `git add .`
will publish this sensitive financial data. Remove or redact the export, use a synthetic fixture
where a sample is needed, and add a scoped ignore rule for local broker-export files.

### P3 — The converted-list summary describes holdings that are not in the list

[`suggested/broker.txt`](../suggested/broker.txt#L2) says it has 25 holdings and reports the
full ARS [redacted] source total. However, line 8 explicitly drops `TGNO4`, and the file
contains only 24 allocation rows (lines 15–38), whose weights are normalized over the remaining
ARS 136,434,132.50. This makes the generated artifact internally misleading and can cause a
reviewer to think a holding was included when it was excluded. Generate the header from the kept
rows (and optionally state the source total separately), or adjust this committed output before
shipping it.

## Verification

- Inspected `git diff HEAD` and untracked files: one modified broker export and one generated
  weights list.
- Parsed the source with `portfolio_tool.parse_broker`: 25 rows, including one local `TGNO4`
  position; 24 CEDEAR rows remain after the documented drop.
- Summed the generated allocation rows: 100.002% (expected rounding drift from three-decimal
  percentages).
- `git diff --check HEAD` reports no whitespace errors.
