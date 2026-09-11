import React, { useId } from 'react';
import type { ChartSpec, ChartSeries } from './chartContract';

/**
 * Inline SVG renderer for validated {@link ChartSpec} payloads (issue #255).
 *
 * Design goals:
 *   - No charting dependency. Bar (grouped), line, and stacked-area shapes are
 *     drawn with hand-written SVG so there is no third-party runtime to audit,
 *     pin, or theme around.
 *   - Theme-following. Series colors and chrome reference the app's CSS custom
 *     properties (`--ga-chart-*`, `--ga-text-muted`, `--ga-border`), so charts
 *     track the light/dark theme instead of shipping their own palette.
 *   - Not colour-only. Series are also distinguished by non-colour encodings —
 *     dash patterns + marker shapes for lines, hatch/texture patterns for bars
 *     and areas — so the chart is legible to colour-blind readers and in
 *     greyscale. The legend shows the same encoding.
 *   - Accessible + copy-pasteable. The title is the figure caption, the summary
 *     is a described-by paragraph (announced once, not duplicated in the image
 *     label), and a collapsible data table gives a screen-reader and
 *     copy-paste friendly text equivalent of every value with its decimals.
 *   - Readable on narrow screens. The SVG scales to the column but keeps a
 *     minimum width and scrolls horizontally rather than shrinking axis text to
 *     illegibility; x labels are thinned and truncated to the space available.
 *   - Bounded + defensive. Geometry is computed from contract-bounded numbers;
 *     any non-finite coordinate falls back to the text/table equivalent rather
 *     than emitting broken SVG.
 *
 * This component only ever renders numbers as geometry and strings as text
 * nodes; it never renders model-provided markup or executes model-provided
 * code.
 */

interface ChartRendererProps {
  spec: ChartSpec;
}

// Logical drawing surface. Scaled to the container via CSS (width: 100%). The
// bottom/left margins leave room for tick labels AND the optional axis titles.
const VIEW_WIDTH = 640;
const VIEW_HEIGHT = 360;
const MARGIN = { top: 16, right: 18, bottom: 64, left: 68 };
const PLOT_WIDTH = VIEW_WIDTH - MARGIN.left - MARGIN.right;
const PLOT_HEIGHT = VIEW_HEIGHT - MARGIN.top - MARGIN.bottom;
const Y_TICK_COUNT = 4;
const MAX_TICKS = 64; // Hard cap so a degenerate step can never spin the loop.
// Eight-color palette defined for both themes in globals.css.
const SERIES_COLOR_COUNT = 8;
// Above this many x points we stop drawing per-point markers on lines so a
// dense series doesn't emit a marker per pixel.
const MARKER_DENSITY_LIMIT = 40;
// Minimum logical width budget for one x-tick label; drives how many we show.
const MIN_LABEL_SLOT = 52;
// Approximate logical width of one character at the tick font size.
const CHAR_WIDTH = 6.5;

// Non-colour encodings, indexed to match the colour palette.
const DASH_PATTERNS = ['0', '7 4', '2 3', '9 3 2 3', '12 4', '1 4', '7 3 1 3', '4 3'];
const MARKER_SHAPES = ['circle', 'square', 'triangle', 'diamond', 'triangleDown', 'cross', 'ex', 'hexagon'] as const;

function seriesColor(index: number): string {
  return `var(--ga-chart-${(index % SERIES_COLOR_COUNT) + 1})`;
}

function dashFor(index: number): string {
  return DASH_PATTERNS[index % DASH_PATTERNS.length];
}

function markerShapeFor(index: number): (typeof MARKER_SHAPES)[number] {
  return MARKER_SHAPES[index % MARKER_SHAPES.length];
}

// ---------------------------------------------------------------------------
// Number formatting (issue #255, finding 2): unit-aware, precision-aware.
// ---------------------------------------------------------------------------

function isCurrencyUnit(unit?: string): boolean {
  return unit === 'USD' || unit === '$';
}

// Table formatting bounds (issue #255, finding 3): the data table is the
// accessible, copy-pasteable text equivalent of the chart, so it MUST preserve
// a ROUND-TRIPPABLE representation of every value — the decimal it shows must
// parse back to the exact same JS number. It therefore never truncates to a
// fixed number of decimals; it renders the value's own precision, derived from
// Number.toString() (the shortest string that round-trips).
//   - A nonzero magnitude below this would collapse to a fixed-point "0" (its
//     significant digits are all beyond the point where fixed notation stays
//     legible), so it is rendered in scientific notation instead — the value's
//     significance and sign survive for screen readers and copy-paste. 1e-6
//     (one micro-unit) is the smallest value the fixed path can still show
//     clearly, so anything smaller is a "sub-micro" value shown scientifically.
const TABLE_SCIENTIFIC_THRESHOLD = 1e-6;
//   - Ceiling on fixed-point fraction digits passed to Intl.NumberFormat. This
//     is Intl's documented maximum (0–100), NOT a precision budget: a
//     fixed-path value (abs ≥ TABLE_SCIENTIFIC_THRESHOLD and ≤ MAX_ABS_VALUE)
//     never carries more than ~22 fraction digits in its shortest
//     round-trippable form, so this ceiling never limits precision — it only
//     keeps the Intl argument inside the legal range.
const TABLE_MAX_FRACTION_DIGITS = 100;

/** Count fractional digits in a finite number, capped so formatting stays bounded. */
function decimalsOf(value: number): number {
  if (!Number.isFinite(value) || Number.isInteger(value)) return 0;
  const text = Math.abs(value).toString();
  if (text.includes('e-')) {
    return Math.min(6, Number(text.split('e-')[1]) || 0);
  }
  const fraction = text.split('.')[1] ?? '';
  return Math.min(6, fraction.length);
}

/**
 * Fraction digits a value carries in its shortest round-trippable form
 * (Number.toString()), bounded only by {@link TABLE_MAX_FRACTION_DIGITS} — the
 * Intl ceiling, never reached in practice. Preserving this exact count (rather
 * than truncating to a fixed number of decimals) is what lets the table
 * round-trip values with more than a dozen fractional digits, e.g.
 * `1.0000000000001` or `0.000001000000000001` (issue #255, finding 3). Only
 * used on the fixed-point path (abs ≥ {@link TABLE_SCIENTIFIC_THRESHOLD} and
 * ≤ MAX_ABS_VALUE), whose values never stringify with an exponent, so the
 * `'.'`-split below sees the full decimal expansion.
 */
function tableFractionDigitsOf(value: number): number {
  if (!Number.isFinite(value) || Number.isInteger(value)) return 0;
  const text = Math.abs(value).toString();
  if (text.includes('e-')) {
    const [mantissa, exponent] = text.split('e-');
    const mantissaDecimals = mantissa.split('.')[1]?.length ?? 0;
    return Math.min(TABLE_MAX_FRACTION_DIGITS, mantissaDecimals + (Number(exponent) || 0));
  }
  const fraction = text.split('.')[1] ?? '';
  return Math.min(TABLE_MAX_FRACTION_DIGITS, fraction.length);
}

/** Fraction digits implied by an axis step (e.g. step 0.25 → 2, step 5 → 0). */
function decimalsFromStep(step: number): number {
  return decimalsOf(step);
}

function trimTrailingZeros(fixed: string): string {
  return String(Number(fixed));
}

// Axis ticks whose magnitude is below this are labelled scientifically at
// DISPLAY time (issue #255, finding 2). The threshold mirrors the table's
// TABLE_SCIENTIFIC_THRESHOLD so the chart and its data table agree on when a
// value is "sub-micro". The tick's numeric value is NOT rounded — only its
// label is condensed — so flat/tiny domains keep unique, finite coordinates.
const TICK_SCIENTIFIC_THRESHOLD = 1e-6;
// Significant digits shown in a scientific tick label. This bounds the LABEL
// only; nice steps are 1/2/5×10ⁿ, so three fraction digits after rounding is
// ample and trailing zeros are trimmed away for a clean "2e-7"-style label.
const TICK_SCIENTIFIC_SIG_DIGITS = 3;

/**
 * Condensed scientific label for a small positive magnitude. Rounds for DISPLAY
 * (the geometry uses the unrounded tick value) and trims trailing mantissa
 * zeros so a float-drifted `4.0000000000000003e-7` prints as `4e-7`.
 */
function formatScientificTick(abs: number): string {
  const [mantissa, exponent] = abs.toExponential(TICK_SCIENTIFIC_SIG_DIGITS).split('e');
  const cleanMantissa = mantissa.includes('.') ? String(Number(mantissa)) : mantissa;
  return `${cleanMantissa}e${exponent}`;
}

/**
 * Decorate a bare numeric string with currency / percent / unit semantics,
 * keeping any leading `-` outside the currency symbol (`-$4e-7`).
 */
function decorateNumber(body: string, unit: string | undefined, currency: boolean): string {
  const negative = body.startsWith('-');
  const bare = negative ? body.slice(1) : body;
  const sign = negative ? '-' : '';
  if (currency) return `${sign}$${bare}`;
  if (unit === '%') return `${sign}${bare}%`;
  return unit ? `${sign}${bare} ${unit}` : `${sign}${bare}`;
}

/**
 * Format a measured value for the data table. Preserves the value's own
 * decimals (so 3.14 does not collapse to 3), applies currency/percent/unit
 * semantics, and groups thousands via the platform locale.
 *
 * Crucially (issue #255, finding 3) it never lets a nonzero value display as
 * zero and never truncates precision: a nonzero sub-micro magnitude — which
 * fixed-point notation would round away — is shown in scientific notation with
 * as many significant digits as the value needs to round-trip, so its exact
 * value, significance, and sign survive in the accessible / copy-pasteable
 * text.
 */
function formatMeasured(value: number, unit?: string): string {
  // Defensive: values are contract-validated finite upstream; if a non-finite
  // value ever reaches here, surface it as text rather than a misleading "0".
  if (!Number.isFinite(value)) return String(value);

  const currency = isCurrencyUnit(unit);
  const abs = Math.abs(value);

  if (value !== 0 && abs < TABLE_SCIENTIFIC_THRESHOLD) {
    // No-argument toExponential() emits the SHORTEST exponential string that
    // parses back to the same double — "sufficient significant digits" rather
    // than a fixed truncation — so tiny values round-trip exactly (e.g.
    // 1.2345678e-9 keeps all its digits, not just four).
    return decorateNumber(value.toExponential(), unit, currency);
  }

  const minimumFractionDigits = currency ? 2 : 0;
  const maximumFractionDigits = Math.max(minimumFractionDigits, tableFractionDigitsOf(value));

  if (currency) {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits,
      maximumFractionDigits,
    }).format(value);
  }

  const formatted = new Intl.NumberFormat(undefined, {
    minimumFractionDigits,
    maximumFractionDigits,
  }).format(value);

  if (unit === '%') return `${formatted}%`;
  return unit ? `${formatted} ${unit}` : formatted;
}

/**
 * Compact axis tick label. Precision comes from the axis STEP (not the raw
 * value), large magnitudes collapse to k/M, very small magnitudes collapse to
 * scientific notation, and only compact glyph units ($, %) are shown inline —
 * any other unit is surfaced once on the axis title instead of on every tick.
 *
 * This is a DISPLAY-only transform (issue #255, finding 2): the numeric tick
 * value handed to the geometry is never rounded here, so a tiny/flat domain
 * keeps unique, finite plot coordinates — only the label text is condensed.
 */
function formatTick(value: number, unit: string | undefined, stepDecimals: number): string {
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  let body: string;
  if (abs >= 1_000_000) {
    body = `${trimTrailingZeros((abs / 1_000_000).toFixed(1))}M`;
  } else if (abs >= 10_000) {
    body = `${trimTrailingZeros((abs / 1_000).toFixed(1))}k`;
  } else if (abs !== 0 && abs < TICK_SCIENTIFIC_THRESHOLD) {
    // A sub-micro tick would render as a bare "0" (or leading-zero mush) in
    // fixed point; show it scientifically so the axis stays readable and no two
    // distinct ticks share the label "0".
    body = formatScientificTick(abs);
  } else {
    body = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: Math.min(6, stepDecimals),
    }).format(abs);
  }
  if (isCurrencyUnit(unit)) return `${sign}$${body}`;
  if (unit === '%') return `${sign}${body}%`;
  return `${sign}${body}`;
}

function truncateLabel(label: string, maxChars: number): string {
  if (maxChars <= 1 || label.length <= maxChars) return label;
  if (maxChars <= 1) return '…';
  return `${label.slice(0, maxChars - 1)}…`;
}

/** "Nice" rounded step for axis ticks given a raw span. */
function niceStep(span: number, tickCount: number): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const rough = span / tickCount;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const nice = normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1;
  return nice * magnitude;
}

interface YDomain {
  min: number;
  max: number;
  step: number;
  ticks: number[];
}

/**
 * y coordinate for a value under the plot scale. This is the single source of
 * truth for the vertical mapping: {@link makeScales} builds the renderer's
 * `yFor` from it, and {@link uniqueFiniteTicks} uses it to deduplicate ticks in
 * the SAME pixel space the renderer will draw in, so "unique coordinate" means
 * exactly the coordinate that ends up on the axis. A zero-width domain falls
 * back to a unit range so the map stays finite.
 */
function plotYFor(value: number, min: number, max: number): number {
  const yRange = max - min || 1;
  return MARGIN.top + PLOT_HEIGHT - ((value - min) / yRange) * PLOT_HEIGHT;
}

/**
 * Reduce candidate tick values to those that map to DISTINCT, finite, in-plot y
 * coordinates under the given scale (issue #255, ULP-boundary duplicate ticks).
 *
 * Two independent hazards are screened, and deduplicating by the resulting
 * coordinate covers both at once:
 *   - ULP collapse in the value domain: when a "nice" step is smaller than the
 *     domain's own ULP (e.g. a step of 5e-23 against values near 1e-6, whose
 *     ULP is ~2.1e-22), `min + i*step` snaps several consecutive candidates to
 *     the SAME double — duplicate numeric values, hence duplicate coordinates
 *     and grid lines stacked on top of each other.
 *   - Pixel collapse in the coordinate domain: two genuinely distinct values
 *     can still land on one coordinate if the domain is wide enough that their
 *     pixel delta underflows.
 *
 * Numeric values are preserved UNROUNDED (formatting stays display-only, issue
 * #255, finding 2); only a floating `-0` is normalised to `0` for a stable
 * label. The first candidate to claim a coordinate wins; later duplicates and
 * any non-finite value/coordinate are dropped.
 */
function uniqueFiniteTicks(values: number[], min: number, max: number): number[] {
  const seenCoordinates = new Set<number>();
  const ticks: number[] = [];
  for (const raw of values) {
    const value = Object.is(raw, -0) ? 0 : raw;
    if (!Number.isFinite(value)) continue;
    const y = plotYFor(value, min, max);
    if (!Number.isFinite(y) || seenCoordinates.has(y)) continue;
    seenCoordinates.add(y);
    ticks.push(value);
  }
  return ticks;
}

/**
 * Build a representable y domain for a finite, contract-valid domain so small
 * that {@link niceStep} underflows to 0 / a non-finite value (issue #255).
 *
 * The canonical trigger is a subnormal-scale input such as `[Number.MIN_VALUE]`
 * or `[0, Number.MIN_VALUE]`: `span / tickCount` rounds below the smallest
 * positive double, so `Math.pow(10, floor(log10(rough)))` underflows to 0 and
 * `niceStep` returns 0. A zero step must NEVER reach the candidate loop — it
 * would emit `niceMin + i*0` (a single stacked coordinate) or, worse, drive a
 * degenerate iteration count. Instead we anchor a domain on the data extremes:
 *   - all-positive (min ≥ 0, max > 0): `[0, maxValue]` so the bar/line grows
 *     from a zero baseline;
 *   - all-negative (max ≤ 0, min < 0): `[minValue, 0]`, the mirror image;
 *   - mixed sign: the raw `[minValue, maxValue]` extremes verbatim.
 * (A flat all-zero series never reaches here — its padded domain yields a
 * finite nice step — so it keeps using the existing flat-domain fallback.)
 *
 * Candidate ticks (the endpoints and their midpoint) are pushed through the
 * SAME {@link uniqueFiniteTicks} coordinate deduper the normal path uses. If
 * fewer than two distinct, finite, in-plot coordinates survive (e.g. the
 * endpoints collapse to one pixel), we return null so the caller renders the
 * table equivalent instead of a single-line axis. `step` is reported as the
 * finite span so downstream label-precision logic stays bounded; sub-micro
 * ticks are labelled scientifically regardless.
 */
function subnormalFallbackDomain(dataMin: number, dataMax: number): YDomain | null {
  let min: number;
  let max: number;
  if (dataMin >= 0 && dataMax > 0) {
    min = 0;
    max = dataMax;
  } else if (dataMax <= 0 && dataMin < 0) {
    min = dataMin;
    max = 0;
  } else {
    min = dataMin;
    max = dataMax;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return null;

  const center = min + (max - min) / 2;
  const ticks = uniqueFiniteTicks([min, center, max], min, max);
  if (ticks.length < 2) return null;

  return { min, max, step: max - min, ticks };
}

/**
 * Compute the y domain and tick set. Returns null if the data or the derived
 * geometry is not finite, so the caller can fall back to the text equivalent
 * instead of drawing a broken axis.
 */
function computeYDomain(spec: ChartSpec): YDomain | null {
  const isStacked = spec.type === 'area';
  const pointCount = spec.x.values.length;

  let dataMin = Infinity;
  let dataMax = -Infinity;

  if (isStacked) {
    for (let i = 0; i < pointCount; i += 1) {
      let sum = 0;
      for (const series of spec.series) sum += series.values[i];
      if (!Number.isFinite(sum)) return null; // stacked aggregate overflowed
      dataMax = Math.max(dataMax, sum);
      dataMin = Math.min(dataMin, 0, sum);
    }
  } else {
    for (const series of spec.series) {
      for (const value of series.values) {
        dataMin = Math.min(dataMin, value);
        dataMax = Math.max(dataMax, value);
      }
    }
  }

  if (!Number.isFinite(dataMin) || !Number.isFinite(dataMax)) {
    dataMin = 0;
    dataMax = 1;
  }

  // Bar and stacked-area charts must include a zero baseline to avoid
  // exaggerating differences. Line charts preserve the data's shape.
  let min = dataMin;
  let max = dataMax;
  if (spec.type === 'bar' || isStacked) {
    min = Math.min(0, dataMin);
    max = Math.max(0, dataMax);
  }
  if (min === max) {
    // Flat series: pad so the line/bars are visible.
    const pad = Math.abs(min) || 1;
    min -= pad;
    max += pad;
  }

  const step = niceStep(max - min, Y_TICK_COUNT);
  if (!Number.isFinite(step) || step <= 0) {
    // niceStep underflowed on a finite, contract-valid subnormal-scale domain
    // (e.g. [Number.MIN_VALUE]). Never enter the candidate loop with a zero
    // step; build a representable fallback domain from the data extremes and
    // let its coordinate dedup decide chart-vs-table.
    return subnormalFallbackDomain(dataMin, dataMax);
  }
  const niceMin = Math.floor(min / step) * step;
  const niceMax = Math.ceil(max / step) * step;
  if (!Number.isFinite(niceMin) || !Number.isFinite(niceMax)) return null;

  // Generate candidate ticks (issue #255): build every `niceMin + i*step` up
  // front (not a running `+= step`, which would accumulate drift), then reduce
  // to a set with unique, finite, in-plot coordinates. Near a ULP boundary the
  // step can be below the domain's ULP, so consecutive candidates snap to the
  // same double and would otherwise stack grid lines at one y — deduping by the
  // resulting coordinate removes those without rounding the numeric values.
  const candidates: number[] = [];
  const tickCount = Math.min(MAX_TICKS - 1, Math.max(0, Math.round((niceMax - niceMin) / step)));
  for (let i = 0; i <= tickCount; i += 1) {
    candidates.push(niceMin + i * step);
  }

  let ticks = uniqueFiniteTicks(candidates, niceMin, niceMax);

  // Fallback ONLY when the deduped set can no longer make a useful axis (fewer
  // than two distinct coordinates, e.g. a domain so narrow the whole nice grid
  // collapsed to a single ULP). Synthesise ticks from the finite domain
  // endpoints and their center; these map to the bottom, middle, and top of the
  // plot, so at least two survive coordinate dedup whenever the endpoints
  // themselves are distinct in pixel space.
  if (ticks.length < 2) {
    const center = niceMin + (niceMax - niceMin) / 2;
    ticks = uniqueFiniteTicks([niceMin, center, niceMax], niceMin, niceMax);
  }

  // Still degenerate (endpoints share a coordinate): refuse to draw a
  // single-line axis and let the caller render the table equivalent instead.
  if (ticks.length < 2) return null;

  return { min: niceMin, max: niceMax, step, ticks };
}

function makeScales(domain: YDomain, pointCount: number) {
  const { min, max } = domain;
  // Single source of truth with the tick deduper (see plotYFor).
  const yFor = (value: number) => plotYFor(value, min, max);
  // Band center for category i.
  const band = PLOT_WIDTH / pointCount;
  const bandCenter = (i: number) => MARGIN.left + band * i + band / 2;
  return { yFor, band, bandCenter };
}

// ---------------------------------------------------------------------------
// Non-colour texture patterns for bar/area fills (issue #255, finding 8).
// Rendered once into a shared, hidden <defs> and referenced by id so both the
// chart body and the legend swatches show the same texture.
// ---------------------------------------------------------------------------

function patternTexture(index: number): React.ReactNode {
  const stroke = 'rgba(0,0,0,0.55)';
  const sw = 1.4;
  switch (index % 8) {
    case 0:
      return <path d="M0 8 L8 0" stroke={stroke} strokeWidth={sw} />;
    case 1:
      return <path d="M0 0 L8 8" stroke={stroke} strokeWidth={sw} />;
    case 2:
      return <path d="M0 4 L8 4" stroke={stroke} strokeWidth={sw} />;
    case 3:
      return <path d="M4 0 L4 8" stroke={stroke} strokeWidth={sw} />;
    case 4:
      return (
        <path d="M0 8 L8 0 M0 0 L8 8" stroke={stroke} strokeWidth={sw} />
      );
    case 5:
      return <circle cx={4} cy={4} r={1.4} fill={stroke} />;
    case 6:
      return <path d="M0 4 L8 4 M4 0 L4 8" stroke={stroke} strokeWidth={sw} />;
    default:
      return <path d="M0 6 L6 0 M2 8 L8 2" stroke={stroke} strokeWidth={sw} />;
  }
}

function SeriesPatternDefs({ idPrefix, count }: { idPrefix: string; count: number }) {
  return (
    <svg width={0} height={0} aria-hidden="true" focusable="false" style={{ position: 'absolute' }}>
      <defs>
        {Array.from({ length: count }, (_, i) => (
          <pattern
            key={i}
            id={`${idPrefix}-p${i}`}
            patternUnits="userSpaceOnUse"
            width={8}
            height={8}
          >
            <rect width={8} height={8} fill={seriesColor(i)} fillOpacity={0.6} />
            <g style={{ color: seriesColor(i) }}>{patternTexture(i)}</g>
          </pattern>
        ))}
      </defs>
    </svg>
  );
}

function Marker({ shape, cx, cy, color }: { shape: (typeof MARKER_SHAPES)[number]; cx: number; cy: number; color: string }) {
  const r = 3;
  switch (shape) {
    case 'square':
      return <rect className="ga-chart__marker" x={cx - r} y={cy - r} width={r * 2} height={r * 2} fill={color} />;
    case 'triangle':
      return <polygon className="ga-chart__marker" points={`${cx},${cy - r} ${cx + r},${cy + r} ${cx - r},${cy + r}`} fill={color} />;
    case 'triangleDown':
      return <polygon className="ga-chart__marker" points={`${cx},${cy + r} ${cx + r},${cy - r} ${cx - r},${cy - r}`} fill={color} />;
    case 'diamond':
      return <polygon className="ga-chart__marker" points={`${cx},${cy - r} ${cx + r},${cy} ${cx},${cy + r} ${cx - r},${cy}`} fill={color} />;
    case 'cross':
      return (
        <path className="ga-chart__marker" d={`M${cx - r} ${cy} H${cx + r} M${cx} ${cy - r} V${cy + r}`} stroke={color} strokeWidth={2} />
      );
    case 'ex':
      return (
        <path className="ga-chart__marker" d={`M${cx - r} ${cy - r} L${cx + r} ${cy + r} M${cx - r} ${cy + r} L${cx + r} ${cy - r}`} stroke={color} strokeWidth={2} />
      );
    case 'hexagon': {
      const pts = [0, 1, 2, 3, 4, 5]
        .map((k) => {
          const a = (Math.PI / 3) * k - Math.PI / 6;
          return `${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`;
        })
        .join(' ');
      return <polygon className="ga-chart__marker" points={pts} fill={color} />;
    }
    case 'circle':
    default:
      return <circle className="ga-chart__marker" cx={cx} cy={cy} r={r} fill={color} />;
  }
}

interface BodyProps {
  spec: ChartSpec;
  domain: YDomain;
  idPrefix: string;
}

function BarChart({ spec, domain, idPrefix }: BodyProps) {
  const pointCount = spec.x.values.length;
  const { yFor, band, bandCenter } = makeScales(domain, pointCount);
  const zeroY = yFor(0);
  const groupCount = spec.series.length;
  // Leave 20% of the band as padding between category groups.
  const groupWidth = band * 0.8;
  const barWidth = groupWidth / groupCount;

  return (
    <g>
      {spec.series.map((series, si) =>
        series.values.map((value, i) => {
          const groupLeft = bandCenter(i) - groupWidth / 2;
          const x = groupLeft + si * barWidth;
          const y = value >= 0 ? yFor(value) : zeroY;
          const height = Math.abs(yFor(value) - zeroY);
          return (
            <rect
              key={`${si}-${i}`}
              className="ga-chart__bar"
              x={x}
              y={y}
              width={Math.max(barWidth - 1, 1)}
              height={Math.max(height, 0)}
              fill={`url(#${idPrefix}-p${si})`}
              stroke={seriesColor(si)}
              strokeWidth={0.75}
              rx={1}
            />
          );
        })
      )}
    </g>
  );
}

function LineChart({ spec, domain }: BodyProps) {
  const pointCount = spec.x.values.length;
  const { yFor, bandCenter } = makeScales(domain, pointCount);
  const showMarkers = pointCount <= MARKER_DENSITY_LIMIT;

  return (
    <g fill="none">
      {spec.series.map((series, si) => {
        const points = series.values.map((value, i) => `${bandCenter(i)},${yFor(value)}`).join(' ');
        const shape = markerShapeFor(si);
        return (
          <g key={si}>
            <polyline
              className="ga-chart__line"
              points={points}
              stroke={seriesColor(si)}
              strokeWidth={2}
              strokeDasharray={dashFor(si)}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
            {showMarkers
              ? series.values.map((value, i) => (
                  <Marker key={i} shape={shape} cx={bandCenter(i)} cy={yFor(value)} color={seriesColor(si)} />
                ))
              : null}
          </g>
        );
      })}
    </g>
  );
}

function AreaChart({ spec, domain, idPrefix }: BodyProps) {
  const pointCount = spec.x.values.length;
  const { yFor, bandCenter } = makeScales(domain, pointCount);
  // Cumulative baselines for stacking. Contract guarantees non-negative values
  // for area charts, so the stack is monotonic.
  const baselines: number[] = new Array(pointCount).fill(0);

  return (
    <g>
      {spec.series.map((series, si) => {
        const topPoints: string[] = [];
        const bottomPoints: string[] = [];
        const tops: number[] = [];
        for (let i = 0; i < pointCount; i += 1) {
          const bottom = baselines[i];
          const top = bottom + series.values[i];
          tops[i] = top;
          topPoints.push(`${bandCenter(i)},${yFor(top)}`);
          bottomPoints.push(`${bandCenter(i)},${yFor(bottom)}`);
        }
        for (let i = 0; i < pointCount; i += 1) baselines[i] = tops[i];
        const path = `${topPoints.join(' L ')} L ${bottomPoints.reverse().join(' L ')}`;
        return (
          <path
            key={si}
            className="ga-chart__area"
            d={`M ${path} Z`}
            fill={`url(#${idPrefix}-p${si})`}
            stroke={seriesColor(si)}
            strokeWidth={1}
          />
        );
      })}
    </g>
  );
}

function LegendSwatch({ spec, index, idPrefix }: { spec: ChartSpec; index: number; idPrefix: string }) {
  const color = seriesColor(index);
  if (spec.type === 'line') {
    return (
      <svg className="ga-chart__legend-swatch" width={22} height={12} aria-hidden="true" focusable="false">
        <line x1={1} y1={6} x2={21} y2={6} stroke={color} strokeWidth={2} strokeDasharray={dashFor(index)} />
        <Marker shape={markerShapeFor(index)} cx={11} cy={6} color={color} />
      </svg>
    );
  }
  return (
    <svg className="ga-chart__legend-swatch" width={16} height={12} aria-hidden="true" focusable="false">
      <rect x={0} y={0} width={16} height={12} rx={2} fill={`url(#${idPrefix}-p${index})`} stroke={color} strokeWidth={0.75} />
    </svg>
  );
}

function DataTable({ spec }: { spec: ChartSpec }) {
  return (
    <div className="ga-chart__table-wrap">
      <table className="ga-chart__table">
        {spec.title ? <caption>{spec.title}</caption> : null}
        <thead>
          <tr>
            <th scope="col">{spec.x.label || 'Category'}</th>
            {spec.series.map((series, si) => (
              <th scope="col" key={si}>
                {series.name}
                {spec.unit ? ` (${spec.unit})` : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {spec.x.values.map((label, i) => (
            <tr key={i}>
              <th scope="row">{label}</th>
              {spec.series.map((series: ChartSeries, si) => (
                <td key={si}>{formatMeasured(series.values[i], spec.unit)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartRenderer({ spec }: ChartRendererProps) {
  const titleId = useId();
  const descId = useId();
  const patternPrefix = useId().replace(/[:]/g, '');
  const domain = computeYDomain(spec);
  const pointCount = spec.x.values.length;
  const showLegend = spec.series.length > 1;
  const usesPatterns = spec.type === 'bar' || spec.type === 'area';

  // Image label describes the chart TYPE and axes only. The title lives in the
  // figcaption and the summary in the described-by paragraph, so neither is
  // repeated here (avoids the screen reader announcing them twice).
  const axisContext = [spec.y?.label || spec.unit, spec.x.label]
    .filter(Boolean)
    .join(' by ');
  const accessibleLabel = axisContext ? `${spec.type} chart of ${axisContext}` : `${spec.type} chart`;

  // Width-aware x-label thinning + truncation so labels never overlap. The SVG
  // scales to the column, so we budget in logical units against the plot width.
  const maxLabels = Math.max(2, Math.floor(PLOT_WIDTH / MIN_LABEL_SLOT));
  const xLabelStride = Math.max(1, Math.ceil(pointCount / maxLabels));
  const labelSlot = (PLOT_WIDTH / pointCount) * xLabelStride;
  const maxLabelChars = Math.max(4, Math.floor(labelSlot / CHAR_WIDTH));

  // Keep dense charts wide enough to stay legible; the wrapper scrolls instead
  // of squeezing axis text to nothing on a narrow screen.
  const svgMinWidth = Math.min(1600, Math.max(320, pointCount * 22));

  const yAxisTitle = spec.y?.label ?? (spec.unit && !isCurrencyUnit(spec.unit) && spec.unit !== '%' ? spec.unit : undefined);
  const stepDecimals = domain ? decimalsFromStep(domain.step) : 0;
  const scales = domain ? makeScales(domain, pointCount) : null;

  return (
    <figure className="ga-chart" role="group" aria-labelledby={spec.title ? titleId : undefined}>
      {usesPatterns ? <SeriesPatternDefs idPrefix={patternPrefix} count={spec.series.length} /> : null}

      {spec.title ? (
        <figcaption className="ga-chart__title" id={titleId}>
          {spec.title}
        </figcaption>
      ) : null}

      {showLegend ? (
        <ul className="ga-chart__legend">
          {spec.series.map((series, si) => (
            <li key={si} className="ga-chart__legend-item">
              <LegendSwatch spec={spec} index={si} idPrefix={patternPrefix} />
              <span className="ga-chart__legend-label">{series.name}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {domain && scales ? (
        <div className="ga-chart__svg-scroll">
          <svg
            className="ga-chart__svg"
            viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label={accessibleLabel}
            aria-describedby={spec.summary ? descId : undefined}
            style={{ minWidth: svgMinWidth }}
          >
            {/* Y grid lines + tick labels */}
            <g className="ga-chart__grid">
              {domain.ticks.map((tick, i) => {
                const y = scales.yFor(tick);
                return (
                  <g key={i}>
                    <line x1={MARGIN.left} y1={y} x2={MARGIN.left + PLOT_WIDTH} y2={y} stroke="var(--ga-border)" strokeWidth={1} />
                    <text x={MARGIN.left - 8} y={y} textAnchor="end" dominantBaseline="middle" className="ga-chart__tick">
                      {formatTick(tick, spec.unit, stepDecimals)}
                    </text>
                  </g>
                );
              })}
            </g>

            {/* Y axis title (finding 9) */}
            {yAxisTitle ? (
              <text
                className="ga-chart__axis-title"
                transform={`translate(16 ${MARGIN.top + PLOT_HEIGHT / 2}) rotate(-90)`}
                textAnchor="middle"
              >
                {yAxisTitle}
              </text>
            ) : null}

            {/* Chart body */}
            {spec.type === 'bar' ? <BarChart spec={spec} domain={domain} idPrefix={patternPrefix} /> : null}
            {spec.type === 'line' ? <LineChart spec={spec} domain={domain} idPrefix={patternPrefix} /> : null}
            {spec.type === 'area' ? <AreaChart spec={spec} domain={domain} idPrefix={patternPrefix} /> : null}

            {/* X axis tick labels (thinned + truncated) */}
            <g className="ga-chart__axis-x">
              {spec.x.values.map((label, i) =>
                i % xLabelStride === 0 ? (
                  <text key={i} x={scales.bandCenter(i)} y={VIEW_HEIGHT - MARGIN.bottom + 18} textAnchor="middle" className="ga-chart__tick">
                    {truncateLabel(label, maxLabelChars)}
                    <title>{label}</title>
                  </text>
                ) : null
              )}
            </g>

            {/* X axis title (finding 9) */}
            {spec.x.label ? (
              <text
                className="ga-chart__axis-title"
                x={MARGIN.left + PLOT_WIDTH / 2}
                y={VIEW_HEIGHT - 6}
                textAnchor="middle"
              >
                {spec.x.label}
              </text>
            ) : null}
          </svg>
        </div>
      ) : (
        <p className="ga-chart__fallback">
          This chart could not be drawn from the provided values. The data is available in the table below.
        </p>
      )}

      {spec.summary ? (
        <p className="ga-chart__summary" id={descId}>
          {spec.summary}
        </p>
      ) : null}

      <details className="ga-chart__data">
        <summary>View data</summary>
        <DataTable spec={spec} />
      </details>
    </figure>
  );
}

export default ChartRenderer;
