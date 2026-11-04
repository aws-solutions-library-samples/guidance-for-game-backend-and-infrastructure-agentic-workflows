import { logInfo, logError, logWarning, redact } from '../../utils/logger'

// Capture the real console sinks the logger writes to.
const originalConsole = { ...console }

beforeEach(() => {
  console.log = jest.fn()
  console.error = jest.fn()
  console.warn = jest.fn()
})

afterEach(() => {
  Object.assign(console, originalConsole)
})

// Synthetic marker containing a CR/LF sequence plus an injected second line.
const CRLF_MARKER = 'request-id-abc\r\nINJECTED-SECOND-LINE'

function emittedText(mockFn: jest.Mock): string {
  // The logger forwards content behind a fixed '%s' specifier; join the
  // remaining string arguments so the assertion sees the emitted message.
  return mockFn.mock.calls
    .flat()
    .filter((arg): arg is string => typeof arg === 'string')
    .join(' ')
}

describe('logger control-character normalization', () => {
  it('logInfo strips CR/LF so one value cannot become two log lines', () => {
    logInfo(CRLF_MARKER)
    const out = emittedText(console.log as jest.Mock)
    expect(out).not.toContain('\r')
    expect(out).not.toContain('\n')
    expect(out).not.toContain('\r\nINJECTED-SECOND-LINE')
  })

  it('logWarning strips CR/LF', () => {
    logWarning(CRLF_MARKER)
    const out = emittedText(console.warn as jest.Mock)
    expect(out).not.toContain('\n')
    expect(out).not.toContain('\r')
  })

  it('logError strips CR/LF from the message', () => {
    logError(CRLF_MARKER)
    const out = emittedText(console.error as jest.Mock)
    expect(out).not.toContain('\n')
    expect(out).not.toContain('\r')
  })

  // Each control / line-separator / bidi code point must be collapsed so a
  // Unicode-aware consumer cannot split one value across lines or reorder it.
  it.each([
    ['NUL', '\u0000'],
    ['ESC', '\u001b'],
    ['DEL', '\u007f'],
    ['NEL (C1)', '\u0085'],
    ['CSI (C1)', '\u009b'],
    ['line separator', '\u2028'],
    ['paragraph separator', '\u2029'],
    ['RTL override', '\u202e'],
    ['LTR isolate', '\u2066'],
  ])('logInfo collapses %s', (_name, codePoint) => {
    logInfo(`a${codePoint}b`)
    const out = emittedText(console.log as jest.Mock)
    expect(out).not.toContain(codePoint)
    expect(out.split(/\r\n|\r|\n|\u2028|\u2029/).length).toBe(1)
  })
})

describe('logError error argument', () => {
  it('does not forward a raw Error object (no stack/message leak in production)', () => {
    const prevEnv = process.env.NODE_ENV
    // Force the production branch regardless of the Jest default.
    Object.defineProperty(process.env, 'NODE_ENV', { value: 'production', configurable: true })
    try {
      const err = new Error('AccessDenied: arn:aws:iam::123456789012:user/ZZSECRET')
      err.name = 'AccessDeniedException'
      ;(err as unknown as { $metadata: { httpStatusCode: number } }).$metadata = { httpStatusCode: 403 }
      logError('operation failed', err)
      const call = (console.error as jest.Mock).mock.calls[0]
      // No argument may be a raw Error object, and the ARN secret must be absent.
      for (const arg of call) {
        expect(arg).not.toBeInstanceOf(Error)
        expect(String(arg)).not.toContain('ZZSECRET')
        expect(String(arg)).not.toContain('arn:aws:iam')
      }
      const joined = call.map((a: unknown) => String(a)).join(' ')
      expect(joined).toContain('AccessDeniedException')
      expect(joined).toContain('403')
    } finally {
      Object.defineProperty(process.env, 'NODE_ENV', { value: prevEnv, configurable: true })
    }
  })

  it('keeps a distinguishing name and safe code for an aws-jwt-verify error', () => {
    const prevEnv = process.env.NODE_ENV
    Object.defineProperty(process.env, 'NODE_ENV', { value: 'production', configurable: true })
    try {
      // aws-jwt-verify error classes leave `name` as the generic "Error", so a
      // bare `error.name` cannot tell an expired token from a bad signature.
      class JwtExpiredError extends Error {
        code = 'ERR_JWT_EXPIRED'
        constructor(message: string) {
          super(message)
          // name intentionally not overridden: it stays "Error".
        }
      }
      const err = new JwtExpiredError('Token expired at 2026-01-01, subject ZZSECRET-SUBJECT')
      expect(err.name).toBe('Error')

      logError('id token verification failed', err)
      const call = (console.error as jest.Mock).mock.calls[0]
      for (const arg of call) {
        expect(arg).not.toBeInstanceOf(Error)
        expect(String(arg)).not.toContain('ZZSECRET-SUBJECT')
      }
      const joined = call.map((a: unknown) => String(a)).join(' ')
      // The concrete class name distinguishes it, and the uppercase code is kept.
      expect(joined).toContain('JwtExpiredError')
      expect(joined).toContain('ERR_JWT_EXPIRED')
    } finally {
      Object.defineProperty(process.env, 'NODE_ENV', { value: prevEnv, configurable: true })
    }
  })

  it('keeps a safe cause.code (fetch failures) but no free-form text', () => {
    const prevEnv = process.env.NODE_ENV
    Object.defineProperty(process.env, 'NODE_ENV', { value: 'production', configurable: true })
    try {
      const err = new Error('fetch failed to backend host ZZSECRET-HOST')
      ;(err as unknown as { cause: { code: string } }).cause = { code: 'ECONNREFUSED' }
      logError('backend call failed', err)
      const call = (console.error as jest.Mock).mock.calls[0]
      const joined = call.map((a: unknown) => String(a)).join(' ')
      expect(joined).not.toContain('ZZSECRET-HOST')
      expect(joined).toContain('ECONNREFUSED')
    } finally {
      Object.defineProperty(process.env, 'NODE_ENV', { value: prevEnv, configurable: true })
    }
  })
})

describe('redact', () => {
  it('does not emit the full value and normalizes control characters', () => {
    const value = 'thread-00000000\r\nINJECTED'
    const out = redact(value)
    expect(out).not.toContain('INJECTED')
    expect(out).not.toContain('\n')
    expect(out).not.toContain('\r')
    expect(out).not.toBe(value)
  })

  it('does not throw on a non-string value and never echoes it in full', () => {
    // A numeric or object threadId arriving in request JSON must not throw.
    expect(() => redact(12345 as unknown as string)).not.toThrow()
    expect(() => redact(['a'] as unknown as string)).not.toThrow()
    expect(() => redact({ x: 1 } as unknown as string)).not.toThrow()
    expect(redact(12345 as unknown as string)).toBe('12…(redacted)')
    expect(redact(null)).toBe('<none>')
    expect(redact(undefined)).toBe('<none>')
  })
})
