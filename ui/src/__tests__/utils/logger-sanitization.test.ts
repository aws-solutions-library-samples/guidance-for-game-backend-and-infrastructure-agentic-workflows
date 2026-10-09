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
})
