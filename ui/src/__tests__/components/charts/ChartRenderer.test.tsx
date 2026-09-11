/**
 * Tests for ChartRenderer (issue #255) — rendering, number formatting,
 * responsiveness, accessibility, and the text/data fallbacks that keep charts
 * screen-reader friendly and copy-paste friendly.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ChartRenderer } from '../../../components/charts/ChartRenderer';
import type { ChartSpec } from '../../../components/charts/chartContract';

const lineSpec: ChartSpec = {
  type: 'line',
  title: 'GameLift spend, last 3 months',
  summary: 'Spend rose from $1,204 in June to $1,880 in August.',
  unit: 'USD',
  x: { label: 'Month', values: ['Jun', 'Jul', 'Aug'] },
  y: { label: 'USD' },
  series: [{ name: 'GameLift', values: [1204, 1521, 1880] }],
};

const barSpec: ChartSpec = {
  type: 'bar',
  title: 'Fleet capacity utilisation',
  x: { label: 'Fleet', values: ['game-agent-demo-a', 'game-agent-demo-b'] },
  series: [
    { name: 'Used', values: [80, 55] },
    { name: 'Capacity', values: [100, 100] },
  ],
};

const areaSpec: ChartSpec = {
  type: 'area',
  title: 'Sessions by region',
  x: { values: ['w1', 'w2', 'w3'] },
  series: [
    { name: 'us-west-2', values: [10, 20, 30] },
    { name: 'eu-west-1', values: [5, 6, 7] },
  ],
};

describe('ChartRenderer — structure', () => {
  it('renders the title and the one-line summary as visible text', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    expect(container.querySelector('.ga-chart__title')).toHaveTextContent('GameLift spend, last 3 months');
    expect(screen.getByText('Spend rose from $1,204 in June to $1,880 in August.')).toBeInTheDocument();
  });

  it('draws one polyline per series for a line chart', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    expect(container.querySelectorAll('.ga-chart__line')).toHaveLength(1);
  });

  it('renders grouped bars (rect per series per category) for a bar chart', () => {
    const { container } = render(<ChartRenderer spec={barSpec} />);
    // 2 series x 2 categories = 4 bars.
    expect(container.querySelectorAll('.ga-chart__bar')).toHaveLength(4);
  });

  it('renders one filled path per series for a stacked area chart', () => {
    const { container } = render(<ChartRenderer spec={areaSpec} />);
    expect(container.querySelectorAll('.ga-chart__area')).toHaveLength(2);
  });

  it('scales responsively via a viewBox rather than a fixed pixel size', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    const svg = container.querySelector('svg.ga-chart__svg');
    expect(svg?.getAttribute('viewBox')).toBeTruthy();
    expect(svg?.getAttribute('width')).toBeNull();
  });

  it('renders a chart with no title/summary without throwing', () => {
    const minimal: ChartSpec = {
      type: 'bar',
      x: { values: ['a', 'b'] },
      series: [{ name: 's', values: [1, 2] }],
    };
    expect(() => render(<ChartRenderer spec={minimal} />)).not.toThrow();
    expect(screen.getByRole('img')).toBeInTheDocument();
  });
});

describe('ChartRenderer — theming', () => {
  it('uses theme palette CSS variables for series colours (no hard-coded palette)', () => {
    const { container } = render(<ChartRenderer spec={barSpec} />);
    // Bars are filled with a texture pattern; the palette colour is carried on
    // the stroke and in the pattern background rect.
    const firstBar = container.querySelector('.ga-chart__bar');
    expect(firstBar?.getAttribute('stroke')).toMatch(/var\(--ga-chart-\d\)/);
    const patternRect = container.querySelector('pattern rect');
    expect(patternRect?.getAttribute('fill')).toMatch(/var\(--ga-chart-\d\)/);
  });
});

describe('ChartRenderer — number formatting (finding 2)', () => {
  it('formats currency, preserving decimals and grouping thousands', () => {
    render(<ChartRenderer spec={lineSpec} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('$1,204.00')).toBeInTheDocument();
    expect(within(table).getByText('$1,880.00')).toBeInTheDocument();
  });

  it('preserves fractional values and renders currency/negative/zero in the table', () => {
    const mixed: ChartSpec = {
      type: 'bar',
      unit: 'USD',
      x: { label: 'Item', values: ['a', 'b', 'c', 'd'] },
      series: [{ name: 'v', values: [1204, 3.14, -5, 0] }],
    };
    render(<ChartRenderer spec={mixed} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('$3.14')).toBeInTheDocument(); // decimals preserved
    expect(within(table).getByText('-$5.00')).toBeInTheDocument(); // negative
    expect(within(table).getByText('$0.00')).toBeInTheDocument();
  });

  it('formats percentage units', () => {
    const pct: ChartSpec = {
      type: 'bar',
      unit: '%',
      x: { values: ['a', 'b'] },
      series: [{ name: 'u', values: [80, 55.5] }],
    };
    render(<ChartRenderer spec={pct} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('80%')).toBeInTheDocument();
    expect(within(table).getByText('55.5%')).toBeInTheDocument();
  });

  it('handles duplicate x labels without collapsing rows', () => {
    const dup: ChartSpec = {
      type: 'line',
      x: { values: ['Jun', 'Jun', 'Jul'] },
      series: [{ name: 's', values: [1, 2, 3] }],
    };
    render(<ChartRenderer spec={dup} />);
    const table = screen.getByRole('table');
    // Two "Jun" row headers survive as distinct rows.
    expect(within(table).getAllByText('Jun')).toHaveLength(2);
  });
});

describe('ChartRenderer — significance-preserving table values (finding 3)', () => {
  it('never renders a nonzero sub-micro value as zero (uses scientific notation)', () => {
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a', 'b', 'c'] },
      // 4e-7 and -4e-7 would round to 0 at six decimals; 0.00012345 needs 8.
      series: [{ name: 'v', values: [0.0000004, -0.0000004, 0.00012345] }],
    };
    render(<ChartRenderer spec={spec} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('4e-7')).toBeInTheDocument();
    expect(within(table).getByText('-4e-7')).toBeInTheDocument();
    // The sub-micro magnitudes must NOT have collapsed to a plain zero.
    expect(within(table).queryByText('0')).not.toBeInTheDocument();
  });

  it('preserves sub-micro precision beyond four significant digits (round-trippable)', () => {
    // A value needing >4 sig digits would be corrupted by a fixed 4-digit
    // scientific truncation; the shortest round-trippable exponential keeps them.
    const value = 1.23456789e-9;
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText(/e-/);
    expect(Number(cell.textContent)).toBe(value); // exact round-trip
  });

  it('retains meaningful precision beyond six decimals for a fixed-point value', () => {
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [0.00012345] }], // 8 significant decimals
    };
    render(<ChartRenderer spec={spec} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('0.00012345')).toBeInTheDocument();
    // Not truncated to the 6-decimal axis precision.
    expect(within(table).queryByText('0.000123')).not.toBeInTheDocument();
  });

  it('preserves sub-micro currency values with sign and symbol', () => {
    const spec: ChartSpec = {
      type: 'bar',
      unit: 'USD',
      x: { values: ['a', 'b'] },
      series: [{ name: 'v', values: [0.0000004, -0.0000004] }],
    };
    render(<ChartRenderer spec={spec} />);
    const table = screen.getByRole('table');
    expect(within(table).getByText('$4e-7')).toBeInTheDocument();
    expect(within(table).getByText('-$4e-7')).toBeInTheDocument();
  });

  it('preserves sub-micro percentage values', () => {
    const spec: ChartSpec = {
      type: 'bar',
      unit: '%',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [0.0000004] }],
    };
    render(<ChartRenderer spec={spec} />);
    expect(within(screen.getByRole('table')).getByText('4e-7%')).toBeInTheDocument();
  });

  it('preserves sub-micro values that carry an arbitrary unit', () => {
    const spec: ChartSpec = {
      type: 'bar',
      unit: 'ms',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [0.0000004] }],
    };
    render(<ChartRenderer spec={spec} />);
    expect(within(screen.getByRole('table')).getByText('4e-7 ms')).toBeInTheDocument();
  });

  it('still renders a genuine zero (and currency zero) as zero', () => {
    const spec: ChartSpec = {
      type: 'bar',
      unit: 'USD',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [0] }],
    };
    render(<ChartRenderer spec={spec} />);
    expect(within(screen.getByRole('table')).getByText('$0.00')).toBeInTheDocument();
  });
});

describe('ChartRenderer — round-trippable table precision beyond 12 decimals (finding 3)', () => {
  // Parse a formatted table cell back to a number, stripping locale grouping
  // separators and any unit/currency/percent decoration, so we can assert the
  // rendered text round-trips to the EXACT input double.
  const parseCell = (text: string): number =>
    Number(text.replace(/[$%,]/g, '').replace(/\s*[A-Za-z]+$/, '').trim());

  it('round-trips a value with 13 fractional digits (bare number)', () => {
    const value = 1.0000000000001;
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText('1.0000000000001');
    expect(parseCell(cell.textContent!)).toBe(value);
  });

  it('round-trips a value with 18 fractional digits (bare number)', () => {
    const value = 0.000001000000000001;
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText('0.000001000000000001');
    expect(parseCell(cell.textContent!)).toBe(value);
  });

  it('round-trips a negative high-precision value', () => {
    const value = -1.0000000000001;
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText('-1.0000000000001');
    expect(parseCell(cell.textContent!)).toBe(value);
  });

  it('round-trips a high-precision value carrying an arbitrary unit', () => {
    const value = 1.0000000000001;
    const spec: ChartSpec = {
      type: 'bar',
      unit: 'ms',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText('1.0000000000001 ms');
    expect(parseCell(cell.textContent!)).toBe(value);
  });

  it('round-trips a high-precision currency value (decoration does not change the number)', () => {
    const value = 1.0000000000001;
    const spec: ChartSpec = {
      type: 'bar',
      unit: 'USD',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [value] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText('$1.0000000000001');
    expect(parseCell(cell.textContent!)).toBe(value);
  });
});

describe('ChartRenderer — accessibility (finding 8)', () => {
  it('exposes the chart as an accessible image labelled by type/axes, not the summary', () => {
    render(<ChartRenderer spec={lineSpec} />);
    const img = screen.getByRole('img');
    expect(img.getAttribute('aria-label')).toMatch(/line chart/);
    // The summary is announced via aria-describedby, NOT duplicated in the label.
    expect(img.getAttribute('aria-label')).not.toContain(lineSpec.summary!);
    const descId = img.getAttribute('aria-describedby');
    expect(descId).toBeTruthy();
    expect(document.getElementById(descId!)?.textContent).toBe(lineSpec.summary);
  });

  it('exposes a textual legend (not aria-hidden) listing every series', () => {
    const { container } = render(<ChartRenderer spec={barSpec} />);
    const legend = container.querySelector('.ga-chart__legend');
    expect(legend).not.toBeNull();
    expect(legend!.getAttribute('aria-hidden')).toBeNull();
    expect(within(legend as HTMLElement).getByText('Used')).toBeInTheDocument();
    expect(within(legend as HTMLElement).getByText('Capacity')).toBeInTheDocument();
  });

  it('distinguishes line series by dash pattern, not colour alone', () => {
    const multi: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b', 'c'] },
      series: [
        { name: 's1', values: [1, 2, 3] },
        { name: 's2', values: [3, 2, 1] },
      ],
    };
    const { container } = render(<ChartRenderer spec={multi} />);
    const lines = container.querySelectorAll('.ga-chart__line');
    expect(lines).toHaveLength(2);
    expect(lines[0].getAttribute('stroke-dasharray')).not.toBe(lines[1].getAttribute('stroke-dasharray'));
  });

  it('shows a legend only when there is more than one series', () => {
    const { container, rerender } = render(<ChartRenderer spec={lineSpec} />);
    expect(container.querySelector('.ga-chart__legend')).toBeNull();
    rerender(<ChartRenderer spec={barSpec} />);
    expect(container.querySelectorAll('.ga-chart__legend-item')).toHaveLength(2);
  });
});

describe('ChartRenderer — axis labels (finding 9)', () => {
  it('renders accepted x and y axis labels in the chart', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    const titles = Array.from(container.querySelectorAll('.ga-chart__axis-title')).map((t) => t.textContent);
    expect(titles).toContain('Month'); // x.label
    expect(titles).toContain('USD'); // y.label
  });
});

describe('ChartRenderer — narrow-screen readability (finding 5)', () => {
  it('wraps the chart in a horizontal-scroll container with a minimum width', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    expect(container.querySelector('.ga-chart__svg-scroll')).not.toBeNull();
    const svg = container.querySelector('svg.ga-chart__svg') as SVGElement;
    expect(svg.style.minWidth).toBeTruthy();
  });

  it('wraps the data table so it can scroll instead of overflowing the column', () => {
    const { container } = render(<ChartRenderer spec={barSpec} />);
    expect(container.querySelector('.ga-chart__table-wrap')).not.toBeNull();
  });

  it('truncates a long x label in the SVG but keeps the full label in the tooltip and table', () => {
    const longLabel = 'this-is-a-very-long-fleet-name-abcdefghijklmnopqrstuv';
    const spec: ChartSpec = {
      type: 'bar',
      x: { label: 'Fleet', values: [longLabel, 'short'] },
      series: [{ name: 'v', values: [1, 2] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    const tick = container.querySelector('.ga-chart__axis-x text');
    // Visible text node is truncated with an ellipsis...
    expect(tick?.firstChild?.nodeValue).toContain('…');
    // ...but the full label remains available in the <title> and the table.
    expect(tick?.querySelector('title')?.textContent).toBe(longLabel);
    expect(within(screen.getByRole('table')).getByText(longLabel)).toBeInTheDocument();
  });
});

describe('ChartRenderer — unrounded tick geometry for tiny/flat domains (finding 2)', () => {
  // Plot geometry mirrors the component: VIEW_HEIGHT 360, top margin 16, bottom
  // margin 64 → the drawable band is y ∈ [16, 296].
  const PLOT_TOP = 16;
  const PLOT_BOTTOM = 296;

  const gridLineYs = (container: HTMLElement): number[] =>
    Array.from(container.querySelectorAll('.ga-chart__grid line')).map((l) =>
      Number(l.getAttribute('y1'))
    );

  const gridLabels = (container: HTMLElement): string[] =>
    Array.from(container.querySelectorAll('.ga-chart__grid text')).map((t) => t.textContent ?? '');

  it('produces finite, unique, in-plot tick coordinates for a flat 4e-7 line domain', () => {
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b', 'c'] },
      series: [{ name: 'v', values: [4e-7, 4e-7, 4e-7] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    const ys = gridLineYs(container);

    expect(ys.length).toBeGreaterThanOrEqual(2);
    expect(ys.every((y) => Number.isFinite(y))).toBe(true);
    // Coordinates are distinct — the old toFixed(6) collapsed them all to one y.
    expect(new Set(ys).size).toBe(ys.length);
    // Every tick sits inside the drawable band (no NaN / off-canvas geometry).
    expect(ys.every((y) => y >= PLOT_TOP - 1e-6 && y <= PLOT_BOTTOM + 1e-6)).toBe(true);
  });

  it('labels sub-micro ticks scientifically, with only the zero tick reading "0"', () => {
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b', 'c'] },
      series: [{ name: 'v', values: [4e-7, 4e-7, 4e-7] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    const labels = gridLabels(container);

    // The flat 4e-7 line domain ticks are 0, 2e-7, 4e-7, 6e-7, 8e-7.
    expect(labels).toContain('2e-7');
    expect(labels).toContain('8e-7');
    // Nonzero ticks are scientific, never a misleading fixed-point "0".
    expect(labels.filter((l) => l === '0')).toHaveLength(1);
    expect(labels.filter((l) => /e-/.test(l)).length).toBeGreaterThanOrEqual(3);
  });

  it('produces finite, unique, in-plot tick coordinates for a flat 4e-7 bar domain', () => {
    const spec: ChartSpec = {
      type: 'bar',
      x: { values: ['a', 'b'] },
      series: [{ name: 'v', values: [4e-7, 4e-7] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    const ys = gridLineYs(container);

    expect(ys.length).toBeGreaterThanOrEqual(2);
    expect(ys.every((y) => Number.isFinite(y))).toBe(true);
    expect(new Set(ys).size).toBe(ys.length);
    expect(ys.every((y) => y >= PLOT_TOP - 1e-6 && y <= PLOT_BOTTOM + 1e-6)).toBe(true);
    // Bar zero-baseline domain ticks are 0, 1e-7, 2e-7, 3e-7, 4e-7.
    expect(gridLabels(container)).toContain('4e-7');
  });

  it('produces finite, unique ticks for a ULP-boundary two-point domain', () => {
    // Two values one ULP apart near 1e-6: the "nice" step (~5e-23) is far below
    // the domain's own ULP (~2.1e-22), so `niceMin + i*step` used to snap five
    // candidate ticks down to two distinct doubles — three grid lines stacked
    // at identical y coordinates. The coordinate deduper must collapse them to a
    // set of unique, finite, in-plot ticks.
    const lo = 0.000001;
    const hi = 0.0000010000000000000002; // lo + 1 ULP
    expect(hi).toBeGreaterThan(lo); // guard: these really are distinct doubles

    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b'] },
      series: [{ name: 'v', values: [lo, hi] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    const ys = gridLineYs(container);

    // Axis stays useful: at least two ticks, none duplicated.
    expect(ys.length).toBeGreaterThanOrEqual(2);
    expect(ys.every((y) => Number.isFinite(y))).toBe(true);
    expect(new Set(ys).size).toBe(ys.length);
    // Every tick has a finite, in-plot coordinate (no NaN / off-canvas geometry).
    expect(ys.every((y) => y >= PLOT_TOP - 1e-6 && y <= PLOT_BOTTOM + 1e-6)).toBe(true);
  });
});

describe('ChartRenderer — subnormal-scale domains draw instead of collapsing (finding 2)', () => {
  // Same plot geometry as the tiny/flat-domain suite: drawable band y ∈ [16, 296].
  const PLOT_TOP = 16;
  const PLOT_BOTTOM = 296;

  const gridLineYs = (container: HTMLElement): number[] =>
    Array.from(container.querySelectorAll('.ga-chart__grid line')).map((l) =>
      Number(l.getAttribute('y1'))
    );

  const expectDrawableAxis = (container: HTMLElement) => {
    const ys = gridLineYs(container);
    // A real axis was drawn — not the "could not be drawn" table-only fallback.
    expect(container.querySelector('.ga-chart__fallback')).toBeNull();
    // At least two ticks, each finite, unique, and inside the drawable band.
    expect(ys.length).toBeGreaterThanOrEqual(2);
    expect(ys.every((y) => Number.isFinite(y))).toBe(true);
    expect(new Set(ys).size).toBe(ys.length);
    expect(ys.every((y) => y >= PLOT_TOP - 1e-6 && y <= PLOT_BOTTOM + 1e-6)).toBe(true);
  };

  it('draws a positive single-point subnormal domain [Number.MIN_VALUE] as [0, max]', () => {
    // niceStep underflows to 0 here; the fallback anchors on a zero baseline so
    // the smallest positive double still maps to a distinct top-of-plot tick.
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [Number.MIN_VALUE] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    expectDrawableAxis(container);
  });

  it('draws a positive two-point subnormal domain [0, Number.MIN_VALUE]', () => {
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b'] },
      series: [{ name: 'v', values: [0, Number.MIN_VALUE] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    expectDrawableAxis(container);
  });

  it('draws a negative single-point subnormal domain [-Number.MIN_VALUE] as [min, 0]', () => {
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a'] },
      series: [{ name: 'v', values: [-Number.MIN_VALUE] }],
    };
    const { container } = render(<ChartRenderer spec={spec} />);
    expectDrawableAxis(container);
  });

  it('keeps every subnormal value round-trippable in the data table', () => {
    // The chart geometry is a zero-anchored approximation, but the accessible
    // table must still carry the exact doubles (scientific, sign-preserving).
    const spec: ChartSpec = {
      type: 'line',
      x: { values: ['a', 'b'] },
      series: [{ name: 'v', values: [0, Number.MIN_VALUE] }],
    };
    render(<ChartRenderer spec={spec} />);
    const cell = within(screen.getByRole('table')).getByText(/e-/);
    expect(Number(cell.textContent)).toBe(Number.MIN_VALUE); // exact round-trip
  });
});

describe('ChartRenderer — density limits (finding 7)', () => {
  it('draws point markers for a sparse line chart', () => {
    const { container } = render(<ChartRenderer spec={lineSpec} />);
    expect(container.querySelectorAll('.ga-chart__marker').length).toBeGreaterThan(0);
  });

  it('omits per-point markers for a dense line chart', () => {
    const values = Array.from({ length: 60 }, (_, i) => i);
    const labels = values.map((v) => `p${v}`);
    const dense: ChartSpec = { type: 'line', x: { values: labels }, series: [{ name: 's', values }] };
    const { container } = render(<ChartRenderer spec={dense} />);
    expect(container.querySelectorAll('.ga-chart__marker')).toHaveLength(0);
    // The line itself is still drawn.
    expect(container.querySelectorAll('.ga-chart__line')).toHaveLength(1);
  });
});
