/**
 * Chart response contract + validation for inline agent charts (issue #255).
 *
 * The agent renders a chart by emitting a fenced code block tagged `chart`
 * whose body is a single JSON object matching {@link ChartSpec}. The chat
 * markdown renderer intercepts that fence, validates the payload here, and — on
 * success — renders it as an inline SVG chart. On ANY validation failure the
 * renderer falls back to showing the raw fenced block, so malformed or
 * untrusted payloads are surfaced as inert text rather than acted on.
 *
 * Security posture (fail closed):
 *   - Only `JSON.parse` is used; model output is never evaluated as code.
 *   - The raw payload is size-capped BEFORE parsing so a hostile megabyte-scale
 *     body can never reach `JSON.parse`.
 *   - The result is accepted only if it matches the contract exactly. Unknown
 *     keys are rejected recursively at every object level (not silently
 *     dropped), so a payload cannot smuggle extra fields past validation.
 *   - Numeric values must be finite AND within a bounded magnitude; strings are
 *     length-capped; series, point, and total-value counts are bounded so a
 *     hostile payload cannot produce a pathological DOM or degenerate geometry.
 *   - Stacked-area charts reject negative values (a stacked baseline is only
 *     well defined for non-negative magnitudes).
 *   - All values are ultimately rendered as text/number nodes (React-escaped)
 *     or numeric SVG geometry — never as HTML.
 *
 * This module is intentionally free of React/DOM imports so the contract can be
 * unit-tested in isolation and reused server-side if ever needed. The backend
 * emits chart fences against the SAME contract (see
 * backend/src/agents/chart_directive.py), so these bounds are the single
 * source of truth for both producer and consumer.
 */

/**
 * Contract version. Bumped when the accepted shape changes in a
 * backward-incompatible way. Emitters MAY include a matching `version` field;
 * the validator accepts an absent or matching-major version and rejects an
 * unknown one, so a future producer speaking a newer dialect fails closed here
 * rather than being mis-rendered.
 */
export const CHART_CONTRACT_VERSION = '1.0';

/** Chart shapes supported today. `map` is a documented future extension. */
export const CHART_TYPES = ['bar', 'line', 'area'] as const;
export type ChartType = (typeof CHART_TYPES)[number];

export interface ChartAxis {
  /** Optional human-readable axis label. */
  label?: string;
  /** Category / time-bucket labels along the x axis. */
  values: string[];
}

export interface ChartSeries {
  /** Series name shown in the legend and data table. */
  name: string;
  /** One numeric value per x-axis category. Length must equal x.values.length. */
  values: number[];
}

export interface ChartSpec {
  type: ChartType;
  /** Optional contract version the payload was produced against. */
  version?: string;
  /** Optional chart title. */
  title?: string;
  /**
   * One-line plain-language reading of the chart. Kept as visible text so the
   * summary survives copy-paste and is available to screen readers even when
   * the SVG is not (issue #255).
   */
  summary?: string;
  /** Optional value unit (e.g. "USD", "%") used for axis + table formatting. */
  unit?: string;
  x: ChartAxis;
  y?: { label?: string };
  series: ChartSeries[];
}

// Bounds. These protect layout and the DOM from pathological payloads while
// comfortably covering the use cases in #255 (a year of daily points; a
// handful of fleets/series compared side by side).
export const MAX_POINTS = 366;
export const MAX_SERIES = 8;
export const MAX_LABEL_LENGTH = 120;
export const MAX_TEXT_LENGTH = 280;
/**
 * Largest raw fenced-block body (in Unicode code points) we will even attempt
 * to parse. A well-formed chart (a year of daily data across 8 series) is a few
 * KB; this cap is generous but keeps a hostile multi-megabyte body away from
 * JSON.parse entirely. Measured in code points (see {@link codePointLengthAtMost})
 * with a strict `>` boundary, kept in sync with the backend directive.
 */
export const MAX_RAW_PAYLOAD_CHARS = 24_000;
/**
 * Maximum absolute value for any numeric datum. Beyond this, tick math and SVG
 * coordinate math lose precision and the axis becomes meaningless. 1e12 covers
 * any realistic cost/utilisation/session figure while staying far inside the
 * safe-integer range used for geometry.
 */
export const MAX_ABS_VALUE = 1e12;
/**
 * Aggregate cap on rendered data points (series × points). Each datum becomes
 * at least one SVG node (rect, or a polyline vertex + optional marker), so this
 * bounds total DOM regardless of how the per-axis budgets are combined. 2000
 * allows a single series of daily data for a year, or 8 series across ~250
 * buckets.
 */
export const MAX_TOTAL_VALUES = 2000;

const SPEC_KEYS = new Set(['type', 'version', 'title', 'summary', 'unit', 'x', 'y', 'series']);
const AXIS_KEYS = new Set(['label', 'values']);
const Y_KEYS = new Set(['label']);
const SERIES_KEYS = new Set(['name', 'values']);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Single length metric shared with the backend: Unicode CODE POINTS (scalar
 * values), NOT UTF-16 code units. `''.length` counts UTF-16 units, so an astral
 * (non-BMP) character — an emoji, a CJK extension ideograph, a mathematical
 * letter — counts as 2 there but as 1 in Python's `len()`. Measuring code
 * points on both sides keeps the exact same string accepted or rejected by both
 * validators (issue #255).
 *
 * The scan early-exits at `max + 1`, so this is bounded work (never longer than
 * the limit) even for a hostile large string, and the `value.length <= max`
 * fast path avoids any scan for the common in-bounds case (UTF-16 length is an
 * upper bound on the code-point count).
 */
export function codePointLengthAtMost(value: string, max: number): boolean {
  if (value.length <= max) return true;
  let count = 0;
  for (let i = 0; i < value.length; ) {
    const code = value.codePointAt(i) as number;
    count += 1;
    if (count > max) return false;
    i += code > 0xffff ? 2 : 1; // advance past an astral char's surrogate pair
  }
  return true;
}

/**
 * True when a raw fenced-block body is within the pre-parse size cap. Uses the
 * shared code-point metric and the same strict `>` boundary as the backend, so
 * a body of exactly `MAX_RAW_PAYLOAD_CHARS` code points is allowed and any
 * larger one is refused identically on both sides.
 */
export function isWithinRawPayloadCap(raw: string): boolean {
  return typeof raw === 'string' && codePointLengthAtMost(raw, MAX_RAW_PAYLOAD_CHARS);
}

/**
 * True when `value` is a plain object whose OWN keys are all in `allowed`.
 * Rejecting (rather than dropping) unknown keys keeps the accepted shape
 * exactly equal to the documented contract. Note `JSON.parse` surfaces a
 * `"__proto__"` payload key as an own enumerable key, so this also rejects
 * prototype-pollution attempts.
 */
function hasOnlyAllowedKeys(value: unknown, allowed: Set<string>): value is Record<string, unknown> {
  if (!isPlainObject(value)) return false;
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) return false;
  }
  return true;
}

function isBoundedString(value: unknown, max: number): value is string {
  return typeof value === 'string' && codePointLengthAtMost(value, max);
}

function isRenderableNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_ABS_VALUE;
}

/** Accept an absent version, or one whose major component matches this contract. */
function isCompatibleVersion(value: unknown): boolean {
  if (value === undefined) return true;
  if (!isBoundedString(value, MAX_LABEL_LENGTH)) return false;
  const major = value.split('.', 1)[0];
  return major === CHART_CONTRACT_VERSION.split('.', 1)[0];
}

/**
 * Parse and validate a raw fenced-block body into a {@link ChartSpec}.
 *
 * Returns the validated spec on success, or `null` for any malformed or
 * out-of-contract payload. Callers MUST treat `null` as "not a chart" and fall
 * back to inert rendering. Never throws.
 */
export function parseChartSpec(raw: string): ChartSpec | null {
  if (typeof raw !== 'string' || raw.trim() === '') {
    return null;
  }
  // Size-cap BEFORE parsing so a hostile large body never reaches JSON.parse.
  if (!isWithinRawPayloadCap(raw)) {
    return null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  return validateChartSpec(parsed);
}

/**
 * Validate an already-parsed value against the chart contract. Exported for
 * direct unit testing of the structural rules.
 */
export function validateChartSpec(input: unknown): ChartSpec | null {
  // Top-level object must have ONLY contract keys (no extras, coerced or not).
  if (!hasOnlyAllowedKeys(input, SPEC_KEYS)) {
    return null;
  }

  const { type, version, title, summary, unit, x, y, series } = input;

  if (typeof type !== 'string' || !(CHART_TYPES as readonly string[]).includes(type)) {
    return null;
  }
  if (!isCompatibleVersion(version)) return null;

  // Optional string fields must be strings when present.
  if (title !== undefined && !isBoundedString(title, MAX_TEXT_LENGTH)) return null;
  if (summary !== undefined && !isBoundedString(summary, MAX_TEXT_LENGTH)) return null;
  if (unit !== undefined && !isBoundedString(unit, MAX_LABEL_LENGTH)) return null;

  // x axis — exact keys, non-empty bounded label list.
  if (!hasOnlyAllowedKeys(x, AXIS_KEYS)) return null;
  if (x.label !== undefined && !isBoundedString(x.label, MAX_LABEL_LENGTH)) return null;
  if (!Array.isArray(x.values) || x.values.length === 0 || x.values.length > MAX_POINTS) {
    return null;
  }
  if (!x.values.every((label) => isBoundedString(label, MAX_LABEL_LENGTH))) {
    return null;
  }
  const pointCount = x.values.length;

  // y axis (optional) — exact keys when present.
  if (y !== undefined) {
    if (!hasOnlyAllowedKeys(y, Y_KEYS)) return null;
    if (y.label !== undefined && !isBoundedString(y.label, MAX_LABEL_LENGTH)) return null;
  }

  // series.
  if (!Array.isArray(series) || series.length === 0 || series.length > MAX_SERIES) {
    return null;
  }
  // Aggregate DOM/geometry guard: bound total rendered data points.
  if (series.length * pointCount > MAX_TOTAL_VALUES) {
    return null;
  }
  const rejectNegative = type === 'area';
  const validatedSeries: ChartSeries[] = [];
  for (const candidate of series) {
    if (!hasOnlyAllowedKeys(candidate, SERIES_KEYS)) return null;
    if (!isBoundedString(candidate.name, MAX_LABEL_LENGTH)) return null;
    if (!Array.isArray(candidate.values) || candidate.values.length !== pointCount) {
      return null;
    }
    if (!candidate.values.every(isRenderableNumber)) return null;
    // Stacked-area geometry is only well defined for non-negative magnitudes;
    // reject negatives clearly rather than drawing an ambiguous stack.
    if (rejectNegative && candidate.values.some((value) => value < 0)) return null;
    validatedSeries.push({ name: candidate.name, values: candidate.values.slice() });
  }

  const spec: ChartSpec = {
    type: type as ChartType,
    x: {
      values: x.values.slice(),
      ...(x.label !== undefined ? { label: x.label } : {}),
    },
    series: validatedSeries,
  };
  if (version !== undefined) spec.version = version as string;
  if (title !== undefined) spec.title = title;
  if (summary !== undefined) spec.summary = summary;
  if (unit !== undefined) spec.unit = unit;
  if (y !== undefined && (y as { label?: string }).label !== undefined) {
    spec.y = { label: (y as { label?: string }).label };
  }

  return spec;
}
