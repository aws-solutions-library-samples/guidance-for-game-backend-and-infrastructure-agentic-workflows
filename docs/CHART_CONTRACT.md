# Inline Chart Contract

The chat frontend can render charts inline in an agent response instead of
describing numbers in prose. This is a **rendering** capability: the agent
already has read access to CloudWatch and Cost Explorer, so no new IAM
permissions are required.

When the answer is quantitative — a change over time, or a comparison across
fleets/regions — the agent emits a fenced code block tagged `chart` whose body
is a single JSON object matching the versioned contract below. The chat markdown
renderer validates that payload and, on success, renders an inline SVG chart.
The agent keeps a one-line text reading of the chart in the message as well, so
the summary survives copy-paste and is available to screen readers.

Contract version: **1.0** (`CHART_CONTRACT_VERSION`). A payload MAY carry a
matching `version` field; the validator accepts an absent or same-major version
and rejects an unknown one (fail closed).

## Where the contract lives (producer + consumer)

The contract is enforced identically on both sides, from one set of bounds:

- **Consumer (frontend):** `ui/src/components/charts/chartContract.ts` validates
  every payload before rendering and **fails closed** — anything out of contract
  falls back to inert text.
- **Producer (trusted backend prompt path):** the versioned instruction that
  tells the model how to emit a chart is part of the **deployed system prompt**
  sent to Amazon Bedrock AgentCore, not only the client-side CopilotChat
  `instructions` prop. It is defined in
  `backend/src/agents/chart_directive.py` (`CHART_DIRECTIVE`) and composed into
  the orchestrator and every specialist prompt by the accessors in
  `backend/src/agents/optimized_prompts.py`. Its version is reported by
  `get_prompt_versions()` for traceability.
- **Deterministic producer (cost reports):** the validated Cost Explorer report
  (`backend/src/agents/cost_report.py`) emits a `chart` fence built directly from
  the same validated, displayed amounts shown in its markdown table. The model
  never sees or reconstructs those numbers, so the chart is exactly consistent
  with the authoritative figures. `render_chart_fence` applies the shared
  contract and returns nothing if a spec would be rejected.

`ui/src/components/charts/chartContract.ts`,
`backend/src/agents/chart_directive.py`, and this document are the single source
of truth for the bounds and must be kept in sync.

### Producer–consumer parity

The backend (`build_chart_spec`) and frontend (`validateChartSpec`) validators
make the **exact same accept/reject decision** for every payload. Three rules
that could otherwise drift are pinned explicitly:

- **Optional nulls are rejected on both sides.** An optional field
  (`version`, `title`, `summary`, `unit`, `x.label`, `y`, `y.label`) may be
  **absent**, but a present-but-`null` value is rejected (fail closed) — it is
  never treated as "absent" or normalized away.
- **One length metric: Unicode code points.** Every string length — the field
  bounds *and* the raw payload cap — is measured in Unicode code points (not
  UTF-16 code units, not bytes), with a strict `>` boundary. An astral (non-BMP)
  character counts as 1 on both sides, so the same string is accepted or
  rejected identically. (Python `len` counts code points; the frontend counts
  them via `codePointLengthAtMost`.)
- **Large integers are never float-converted.** The backend compares integer
  values directly against `MAX_ABS_VALUE` (a Python `int` is arbitrary
  precision, and `float()` on a very large one raises), and checks floats with
  `math.isfinite`. `build_chart_spec` and `render_chart_fence` never raise —
  they fail closed and return `None` / `""`.

This parity is regression-tested from a **shared corpus**,
`docs/chart-contract-parity-corpus.json`, consumed by both the Python validator
test and the TypeScript validator test. It carries null, astral-Unicode, exact
boundary, huge-integer, and out-of-range cases; both validators must agree with
each case's expected decision.

## How it renders

- The renderer is wired through CopilotChat's `markdownTagRenderers` in
  `ui/src/components/Chat.tsx`. The `code` renderer
  (`ui/src/components/MarkdownCodeRenderer.tsx`) intercepts `chart` fences.
- Valid payloads render via `ui/src/components/charts/ChartRenderer.tsx`.
- Charts follow the app's light/dark theme through the `--ga-chart-1..8` CSS
  palette and other `--ga-*` tokens defined in `ui/src/styles/globals.css`.
  They do not ship their own colours.

## Safety

Validation **fails closed**:

- The raw fenced body is **size-capped before parsing** (`MAX_RAW_PAYLOAD_CHARS`,
  24,000 chars), so a hostile large body never reaches `JSON.parse`.
- The body is parsed with `JSON.parse` only. Model output is never evaluated as
  code and never rendered as HTML.
- A payload is accepted only if it matches the contract **exactly**. Unknown
  keys are **rejected recursively at every object level** (spec, `x`, `y`, each
  series) — not silently dropped. This also rejects `__proto__`-style keys.
- Numeric values must be finite **and** within a bounded magnitude
  (`MAX_ABS_VALUE`). Non-finite, out-of-range, oversized, or extra fields are
  rejected — not coerced.
- Stacked-area (`area`) charts **reject negative values**: a stacked baseline is
  only well defined for non-negative magnitudes, so a diverging stack is refused
  clearly rather than drawn ambiguously.
- On any validation failure (including a truncated mid-stream payload), the
  renderer falls back to showing the raw fenced block as inert, readable text.

Bounds enforced by the validator:

| Bound                    | Value  | Reason                                            |
| ------------------------ | ------ | ------------------------------------------------- |
| Max x-axis points        | 366    | Covers a year of daily data                       |
| Max series               | 8      | Matches the eight-colour themed palette           |
| Max total values         | 2000   | series × points; bounds total SVG/DOM nodes       |
| Max label length         | 120    | Code points; keeps axis/legend labels from abusing layout |
| Max title/summary        | 280    | Code points; keeps chrome text bounded            |
| Max absolute value       | 1e12   | Beyond this, tick/coordinate math loses precision |
| Max raw payload chars    | 24,000 | Code points; caps the body before `JSON.parse`    |
| Numeric values           | finite, in range | Rejects `NaN`/`Infinity`/non-numbers and any value (incl. huge ints) beyond `MAX_ABS_VALUE` |
| Area (stacked) values    | ≥ 0    | Negative stacked-area values are rejected         |

The renderer additionally guards geometry: if any computed coordinate would be
non-finite it renders the data table (text equivalent) instead of a broken SVG,
line markers are dropped above a density threshold, and the tick loop is capped.

## Supported chart types

| `type`  | Use for                                        | Multiple series          |
| ------- | ---------------------------------------------- | ------------------------ |
| `line`  | Change over time (trend / shape)               | Overlaid lines           |
| `bar`   | Comparison across fleets/items                 | Grouped bars             |
| `area`  | Composition over time (non-negative)           | Stacked areas            |

`map` (region-based data) is **not** supported yet; it is a documented future
extension of this same contract (add a `map` type and a region/value shape).

## Schema

```jsonc
{
  "type": "line",            // "line" | "bar" | "area"  (required)
  "version": "1.0",          // optional; must match the current major if present
  "title": "…",              // optional chart title
  "summary": "…",            // optional one-line reading (keep it in the prose too)
  "unit": "USD",             // optional: "USD" | "%" | any short unit label
  "x": {
    "label": "Month",        // optional axis label (rendered on the x axis)
    "values": ["Jun", "Jul", "Aug"]   // required: category / time-bucket labels
  },
  "y": { "label": "USD" },   // optional (rendered as the rotated y-axis title)
  "series": [                // required: 1..8 series
    { "name": "GameLift", "values": [1204, 1521, 1880] }
  ]
}
```

Every `series[i].values` array **must** have the same length as `x.values`. No
fields beyond those shown are allowed at any level.

## Examples

All examples below use synthetic data only.

### Line — spend over time

````markdown
GameLift spend rose steadily over the last three months, from $1,204 to $1,880.

```chart
{
  "type": "line",
  "version": "1.0",
  "title": "GameLift spend, last 3 months",
  "summary": "Spend rose from $1,204 in June to $1,880 in August.",
  "unit": "USD",
  "x": { "label": "Month", "values": ["Jun", "Jul", "Aug"] },
  "y": { "label": "USD" },
  "series": [{ "name": "GameLift", "values": [1204, 1521, 1880] }]
}
```
````

### Bar — capacity comparison across fleets

````markdown
`game-agent-demo-a` is closest to max capacity at 80% used.

```chart
{
  "type": "bar",
  "title": "Fleet capacity utilisation",
  "summary": "game-agent-demo-a is at 80% of capacity; game-agent-demo-b is at 55%.",
  "unit": "%",
  "x": { "label": "Fleet", "values": ["game-agent-demo-a", "game-agent-demo-b"] },
  "series": [
    { "name": "Used", "values": [80, 55] },
    { "name": "Capacity", "values": [100, 100] }
  ]
}
```
````

### Area — sessions by region over time

````markdown
Player sessions are growing, with us-west-2 carrying most of the load.

```chart
{
  "type": "area",
  "title": "Player sessions by region",
  "summary": "Sessions are trending up; us-west-2 leads and eu-west-1 is steady.",
  "x": { "label": "Week", "values": ["w1", "w2", "w3", "w4"] },
  "series": [
    { "name": "us-west-2", "values": [120, 150, 180, 210] },
    { "name": "eu-west-1", "values": [40, 45, 50, 55] }
  ]
}
```
````

## Accessibility

- Series are distinguished by **more than colour**: lines use distinct dash
  patterns and marker shapes; bars and areas use distinct SVG texture patterns.
  The legend shows the same encoding, and the legend text (series names) is
  exposed to screen readers.
- The visible one-line `summary` is rendered as a paragraph referenced by the
  chart's `aria-describedby`, and is announced **once** — it is deliberately not
  duplicated into the image's `aria-label`.
- The SVG carries `role="img"` and an `aria-label` describing the chart type and
  axes; the title lives in the figure caption.
- A collapsible **View data** table provides a text equivalent of every value
  (with decimals preserved and unit-aware formatting) for screen readers and for
  copy-pasting into a doc.

## Number formatting

- Axis tick precision is derived from the axis **step**, and large magnitudes
  collapse to `k`/`M`. Only compact glyph units (`$`, `%`) appear inline on
  ticks; any other unit is shown once on the axis title.
- The data table preserves each value's own decimals (e.g. `3.14` stays `3.14`)
  and formats currency, percentage, and other units with `Intl.NumberFormat`.
- The data table never lets a nonzero value display as zero: a nonzero
  **sub-micro** magnitude (below `1e-6`), which fixed-point rounding would
  collapse to `0`, is rendered in **scientific notation** (e.g. `4.000e-7`,
  `-$4.000e-7`, `4.000e-7%`) so its significance and sign survive for screen
  readers and copy-paste. Values that carry more than six decimals retain
  meaningful precision (up to twelve fraction digits) rather than being
  truncated to the coarser axis precision.

## Narrow screens

- The SVG scales to the message column but keeps a minimum width and scrolls
  horizontally rather than shrinking axis text to illegibility.
- X-axis labels are thinned to the available width and truncated with an
  ellipsis (the full label stays in the tooltip and the data table).
- The data table scrolls horizontally within its own container.

## Related

- Producer/consumer parity corpus: `docs/chart-contract-parity-corpus.json`
  (consumed by the Python and TypeScript validator tests)
- Frontend rendering: `ui/src/components/charts/` and
  `ui/src/components/MarkdownCodeRenderer.tsx`
- Backend contract + directive: `backend/src/agents/chart_directive.py`,
  `backend/src/agents/optimized_prompts.py`
- Deterministic cost chart: `backend/src/agents/cost_report.py`
- Theme tokens: `ui/src/styles/globals.css`, `ui/src/styles/chat-layout.css`
