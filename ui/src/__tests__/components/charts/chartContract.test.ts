/**
 * Tests for the inline-chart response contract (issue #255).
 *
 * These lock in the fail-closed validation posture: only payloads that match
 * the contract exactly are accepted; malformed, oversized, out-of-range, or
 * hostile input is rejected (returns null) so the renderer can fall back to
 * inert text.
 */
import {
  parseChartSpec,
  validateChartSpec,
  isWithinRawPayloadCap,
  codePointLengthAtMost,
  CHART_CONTRACT_VERSION,
  MAX_POINTS,
  MAX_SERIES,
  MAX_LABEL_LENGTH,
  MAX_TEXT_LENGTH,
  MAX_ABS_VALUE,
  MAX_TOTAL_VALUES,
  MAX_RAW_PAYLOAD_CHARS,
} from '../../../components/charts/chartContract';
import * as fs from 'fs';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Shared parity corpus (issue #255, finding 1).
//
// The SAME docs/chart-contract-parity-corpus.json is consumed by the Python
// validator test, so the two validators are proven to make the exact same
// decision for optional nulls, the Unicode (code-point) length metric,
// huge/out-of-range numbers, and the raw payload size boundary.
// ---------------------------------------------------------------------------
const PARITY_CORPUS_PATH = path.resolve(__dirname, '../../../../../docs/chart-contract-parity-corpus.json');

interface StrDirective {
  $str: { char: string; count: number };
}
interface SpecCase {
  name: string;
  accept: boolean;
  spec: unknown;
}
interface RawCase {
  name: string;
  char: string;
  count: number;
  withinCap: boolean;
}
interface ParityCorpus {
  lengthMetric: string;
  astralChar: string;
  bounds: Record<string, number>;
  specCases: SpecCase[];
  rawCases: RawCase[];
}

const parityCorpus: ParityCorpus = JSON.parse(fs.readFileSync(PARITY_CORPUS_PATH, 'utf-8'));

/** Expand any {"$str": {char, count}} directive; mirrors the Python loader. */
function materialize(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(materialize);
  if (node && typeof node === 'object') {
    const keys = Object.keys(node as object);
    if (keys.length === 1 && keys[0] === '$str') {
      const { char, count } = (node as StrDirective).$str;
      return char.repeat(count);
    }
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = materialize((node as Record<string, unknown>)[key]);
    return out;
  }
  return node;
}

describe('shared parity corpus (parity with the Python validator)', () => {
  it('corpus bounds match the exported contract constants', () => {
    const { bounds } = parityCorpus;
    expect(bounds.MAX_POINTS).toBe(MAX_POINTS);
    expect(bounds.MAX_SERIES).toBe(MAX_SERIES);
    expect(bounds.MAX_LABEL_LENGTH).toBe(MAX_LABEL_LENGTH);
    expect(bounds.MAX_TEXT_LENGTH).toBe(MAX_TEXT_LENGTH);
    expect(bounds.MAX_ABS_VALUE).toBe(MAX_ABS_VALUE);
    expect(bounds.MAX_RAW_PAYLOAD_CHARS).toBe(MAX_RAW_PAYLOAD_CHARS);
    expect(parityCorpus.lengthMetric).toBe('unicode-code-points');
    // One code point, two UTF-16 units — the whole point of the metric cases.
    expect([...parityCorpus.astralChar]).toHaveLength(1);
    expect(parityCorpus.astralChar.length).toBe(2);
  });

  it.each(parityCorpus.specCases.map((c) => [c.name, c] as const))(
    'spec case %s matches the contract decision',
    (_name, testCase) => {
      const accepted = validateChartSpec(materialize(testCase.spec)) !== null;
      expect(accepted).toBe(testCase.accept);
    }
  );

  it.each(parityCorpus.rawCases.map((c) => [c.name, c] as const))(
    'raw payload case %s matches the cap decision',
    (_name, testCase) => {
      const raw = testCase.char.repeat(testCase.count);
      expect(isWithinRawPayloadCap(raw)).toBe(testCase.withinCap);
    }
  );
});

describe('codePointLengthAtMost — Unicode length metric (finding 1)', () => {
  const astral = '𝔘'; // one code point, two UTF-16 units

  it('counts astral characters as one code point, not two UTF-16 units', () => {
    const label = astral.repeat(MAX_LABEL_LENGTH); // 120 code points, 240 UTF-16 units
    expect(label.length).toBe(MAX_LABEL_LENGTH * 2); // sanity: UTF-16 length differs
    expect(codePointLengthAtMost(label, MAX_LABEL_LENGTH)).toBe(true);
    expect(codePointLengthAtMost(astral.repeat(MAX_LABEL_LENGTH + 1), MAX_LABEL_LENGTH)).toBe(false);
  });

  it('accepts an astral label at the boundary that a UTF-16 metric would wrongly reject', () => {
    const label = astral.repeat(MAX_LABEL_LENGTH);
    expect(
      validateChartSpec({ type: 'line', x: { values: [label] }, series: [{ name: 's', values: [1] }] })
    ).not.toBeNull();
  });

  it('rejects a raw payload over the cap measured in code points, not UTF-16 units', () => {
    // 24000 astral chars = 24000 code points (at cap) but 48000 UTF-16 units.
    expect(isWithinRawPayloadCap(astral.repeat(MAX_RAW_PAYLOAD_CHARS))).toBe(true);
    expect(isWithinRawPayloadCap(astral.repeat(MAX_RAW_PAYLOAD_CHARS + 1))).toBe(false);
  });
});

describe('validateChartSpec — present-null optionals rejected (finding 1)', () => {
  const base = { type: 'line', x: { values: ['a'] }, series: [{ name: 's', values: [1] }] };
  it('rejects an explicit null for every optional field', () => {
    expect(validateChartSpec({ ...base, title: null })).toBeNull();
    expect(validateChartSpec({ ...base, summary: null })).toBeNull();
    expect(validateChartSpec({ ...base, unit: null })).toBeNull();
    expect(validateChartSpec({ ...base, version: null })).toBeNull();
    expect(validateChartSpec({ ...base, x: { label: null, values: ['a'] } })).toBeNull();
    expect(validateChartSpec({ ...base, y: null })).toBeNull();
    expect(validateChartSpec({ ...base, y: { label: null } })).toBeNull();
  });
  it('accepts the same spec when the optionals are simply absent', () => {
    expect(validateChartSpec(base)).not.toBeNull();
    expect(validateChartSpec({ ...base, y: {} })).not.toBeNull();
  });
});

const validLine = {
  type: 'line',
  version: '1.0',
  title: 'GameLift spend, last 3 months',
  summary: 'Spend rose from $1,204 in June to $1,880 in August.',
  unit: 'USD',
  x: { label: 'Month', values: ['Jun', 'Jul', 'Aug'] },
  y: { label: 'USD' },
  series: [{ name: 'GameLift', values: [1204, 1521, 1880] }],
};

describe('parseChartSpec', () => {
  it('parses and validates a well-formed line chart payload', () => {
    const spec = parseChartSpec(JSON.stringify(validLine));
    expect(spec).not.toBeNull();
    expect(spec!.type).toBe('line');
    expect(spec!.series).toHaveLength(1);
    expect(spec!.x.values).toEqual(['Jun', 'Jul', 'Aug']);
    expect(spec!.version).toBe('1.0');
  });

  it('returns null for non-JSON, empty, or non-string input', () => {
    expect(parseChartSpec('not json')).toBeNull();
    expect(parseChartSpec('')).toBeNull();
    expect(parseChartSpec('   ')).toBeNull();
    // @ts-expect-error exercising the runtime guard against non-string input
    expect(parseChartSpec(null)).toBeNull();
  });

  it('does not throw on a truncated (mid-stream) payload', () => {
    const truncated = JSON.stringify(validLine).slice(0, 40);
    expect(() => parseChartSpec(truncated)).not.toThrow();
    expect(parseChartSpec(truncated)).toBeNull();
  });

  it('rejects a raw payload larger than the size cap before parsing', () => {
    // A syntactically valid but enormous body must be rejected without parsing.
    const huge = JSON.stringify({
      type: 'line',
      x: { values: ['a'], label: 'x'.repeat(MAX_LABEL_LENGTH) },
      series: [{ name: 'padding', values: [1] }],
      // pad the (rejected) body well past the cap via a long — but here we just
      // build a big string directly to assert the pre-parse guard.
    });
    const oversized = `${huge}${' '.repeat(MAX_RAW_PAYLOAD_CHARS)}`;
    expect(oversized.length).toBeGreaterThan(MAX_RAW_PAYLOAD_CHARS);
    expect(parseChartSpec(oversized)).toBeNull();
  });

  it('accepts a multi-series grouped bar chart', () => {
    const spec = parseChartSpec(
      JSON.stringify({
        type: 'bar',
        x: { values: ['fleet-a', 'fleet-b'] },
        series: [
          { name: 'Used', values: [80, 55] },
          { name: 'Capacity', values: [100, 100] },
        ],
      })
    );
    expect(spec).not.toBeNull();
    expect(spec!.series).toHaveLength(2);
  });

  it('accepts a stacked area chart with non-negative values', () => {
    const spec = parseChartSpec(
      JSON.stringify({
        type: 'area',
        x: { values: ['w1', 'w2', 'w3'] },
        series: [
          { name: 'us-west-2', values: [10, 20, 30] },
          { name: 'eu-west-1', values: [5, 6, 7] },
        ],
      })
    );
    expect(spec).not.toBeNull();
    expect(spec!.type).toBe('area');
  });
});

describe('validateChartSpec — rejection rules', () => {
  it('rejects unknown chart types', () => {
    expect(validateChartSpec({ ...validLine, type: 'pie' })).toBeNull();
    expect(validateChartSpec({ ...validLine, type: 'map' })).toBeNull();
  });

  it('rejects non-object payloads', () => {
    expect(validateChartSpec(null)).toBeNull();
    expect(validateChartSpec(42)).toBeNull();
    expect(validateChartSpec([validLine])).toBeNull();
    expect(validateChartSpec('string')).toBeNull();
  });

  it('rejects a missing or empty x axis', () => {
    expect(validateChartSpec({ ...validLine, x: undefined })).toBeNull();
    expect(validateChartSpec({ ...validLine, x: { values: [] } })).toBeNull();
    expect(validateChartSpec({ ...validLine, x: { values: 'Jun' } })).toBeNull();
  });

  it('rejects a missing or empty series list', () => {
    expect(validateChartSpec({ ...validLine, series: [] })).toBeNull();
    expect(validateChartSpec({ ...validLine, series: undefined })).toBeNull();
  });

  it('rejects series whose length does not match the x axis', () => {
    expect(
      validateChartSpec({
        ...validLine,
        x: { values: ['Jun', 'Jul', 'Aug'] },
        series: [{ name: 'GameLift', values: [1, 2] }],
      })
    ).toBeNull();
  });

  it('rejects non-finite numeric values (NaN, Infinity, non-number)', () => {
    expect(validateChartSpec({ ...validLine, series: [{ name: 'x', values: [1, 2, NaN] }] })).toBeNull();
    expect(validateChartSpec({ ...validLine, series: [{ name: 'x', values: [1, 2, Infinity] }] })).toBeNull();
    // JSON has no NaN/Infinity literal; a hostile payload might smuggle a string.
    expect(validateChartSpec({ ...validLine, series: [{ name: 'x', values: [1, 2, '3'] }] })).toBeNull();
  });

  it('rejects values whose magnitude exceeds the bound', () => {
    expect(
      validateChartSpec({ ...validLine, series: [{ name: 'x', values: [1, 2, MAX_ABS_VALUE * 10] }] })
    ).toBeNull();
    expect(
      validateChartSpec({ ...validLine, series: [{ name: 'x', values: [1, 2, -MAX_ABS_VALUE * 10] }] })
    ).toBeNull();
  });

  it('rejects a series with a missing or non-string name', () => {
    expect(validateChartSpec({ ...validLine, series: [{ values: [1, 2, 3] }] })).toBeNull();
    expect(validateChartSpec({ ...validLine, series: [{ name: 5, values: [1, 2, 3] }] })).toBeNull();
  });

  it('enforces the point-count bound', () => {
    const values = Array.from({ length: MAX_POINTS + 1 }, (_, i) => i);
    const labels = values.map((v) => `p${v}`);
    expect(
      validateChartSpec({ type: 'line', x: { values: labels }, series: [{ name: 's', values }] })
    ).toBeNull();
  });

  it('enforces the series-count bound', () => {
    const series = Array.from({ length: MAX_SERIES + 1 }, (_, i) => ({ name: `s${i}`, values: [1, 2] }));
    expect(validateChartSpec({ type: 'bar', x: { values: ['a', 'b'] }, series })).toBeNull();
  });

  it('enforces the aggregate total-values bound (series × points)', () => {
    // 8 series × 300 points = 2400 > MAX_TOTAL_VALUES (2000), even though each
    // per-axis bound individually passes.
    const points = 300;
    const labels = Array.from({ length: points }, (_, i) => `p${i}`);
    const values = Array.from({ length: points }, (_, i) => i);
    const series = Array.from({ length: 8 }, (_, i) => ({ name: `s${i}`, values }));
    expect(points * series.length).toBeGreaterThan(MAX_TOTAL_VALUES);
    expect(validateChartSpec({ type: 'line', x: { values: labels }, series })).toBeNull();
  });

  it('enforces the label-length bound', () => {
    const longLabel = 'x'.repeat(MAX_LABEL_LENGTH + 1);
    expect(validateChartSpec({ ...validLine, x: { values: [longLabel, 'Jul', 'Aug'] } })).toBeNull();
  });

  it('rejects unknown keys recursively (spec, x, y, series) — not silently dropped', () => {
    expect(validateChartSpec({ ...validLine, evil: '<script>' })).toBeNull();
    expect(validateChartSpec({ ...validLine, x: { values: ['Jun', 'Jul', 'Aug'], evil: 1 } })).toBeNull();
    expect(validateChartSpec({ ...validLine, y: { label: 'USD', evil: 1 } })).toBeNull();
    expect(
      validateChartSpec({ ...validLine, series: [{ name: 'GameLift', values: [1, 2, 3], evil: 1 }] })
    ).toBeNull();
  });

  it('rejects a prototype-pollution key smuggled through JSON', () => {
    // JSON.parse surfaces "__proto__" as an own enumerable key, which the exact
    // key check rejects.
    const spec = parseChartSpec('{"type":"line","x":{"values":["a"]},"series":[{"name":"s","values":[1]}],"__proto__":{"polluted":true}}');
    expect(spec).toBeNull();
  });

  it('treats the optional y axis as optional but validates it when present', () => {
    expect(validateChartSpec({ ...validLine, y: undefined })).not.toBeNull();
    expect(validateChartSpec({ ...validLine, y: { label: 5 } })).toBeNull();
    expect(validateChartSpec({ ...validLine, y: 'USD' })).toBeNull();
  });

  it('accepts an absent or same-major version but rejects an unknown major', () => {
    const noVersion = { ...validLine };
    delete (noVersion as { version?: string }).version;
    expect(validateChartSpec(noVersion)).not.toBeNull();
    expect(validateChartSpec({ ...validLine, version: `${CHART_CONTRACT_VERSION.split('.')[0]}.9` })).not.toBeNull();
    expect(validateChartSpec({ ...validLine, version: '2.0' })).toBeNull();
    expect(validateChartSpec({ ...validLine, version: 7 })).toBeNull();
  });
});

describe('validateChartSpec — stacked area negative handling (finding 4)', () => {
  it('rejects negative values in a stacked area chart (clear rejection, no ambiguous stack)', () => {
    expect(
      validateChartSpec({
        type: 'area',
        x: { values: ['w1', 'w2'] },
        series: [{ name: 'net', values: [10, -5] }],
      })
    ).toBeNull();
  });

  it('still allows negative values for line and bar charts', () => {
    expect(
      validateChartSpec({ type: 'line', x: { values: ['w1', 'w2'] }, series: [{ name: 'net', values: [10, -5] }] })
    ).not.toBeNull();
    expect(
      validateChartSpec({ type: 'bar', x: { values: ['w1', 'w2'] }, series: [{ name: 'net', values: [10, -5] }] })
    ).not.toBeNull();
  });
});
