import { logInfo, logError, logWarning, logDebug } from '../../utils/logger'

// Mock console methods
const originalConsole = { ...console }

beforeEach(() => {
  console.log = jest.fn()
  console.error = jest.fn()
  console.warn = jest.fn()
  console.info = jest.fn()
})

afterEach(() => {
  Object.assign(console, originalConsole)
})

describe('Logger Utility', () => {
  // Messages are passed behind a fixed '%s' specifier so user-influenced
  // content can't be interpreted as a format string (js/tainted-format-string).
  it('should log messages', () => {
    logInfo('Test message')
    expect(console.log).toHaveBeenCalledWith('%s', 'Test message')
  })

  it('should log errors', () => {
    logError('Test error')
    expect(console.error).toHaveBeenCalledWith('%s', 'Test error', undefined)
  })

  it('should log warnings', () => {
    logWarning('Test warning')
    expect(console.warn).toHaveBeenCalledWith('%s', 'Test warning')
  })

  it('should log debug messages', () => {
    logDebug('Test debug')
    // Debug doesn't log to console, only to file
    expect(console.log).not.toHaveBeenCalled()
  })
})
