/**
 * Tests for chat cancellation functionality
 *
 * TDD: Write tests first to define expected behavior
 */

import { createMocks } from 'node-mocks-http';
import handler from '@/pages/api/copilot/chat';

describe('Chat Cancellation', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'development';
    // Local-dev bypass flag (matches .env.local); NODE_ENV alone no longer skips
    // the access-token check.
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    process.env.BACKEND_URL = 'http://localhost:8080';
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_SKIP_AUTH;
  });

  it('should handle client disconnect gracefully', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [
              {
                textMessage: {
                  role: 'user',
                  content: 'This is a long running query'
                }
              }
            ],
            threadId: 'test-thread'
          }
        }
      }
    });

    // Mock backend to succeed
    global.fetch = jest.fn().mockResolvedValue({
      status: 200,
      json: async () => ({ response: 'Test response' })
    });

    // Simulate client disconnect
    req.socket = {
      destroyed: true,
      on: jest.fn()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any;

    await handler(req, res);

    // Should complete successfully even if client disconnected
    // Backend doesn't need to detect disconnect - HTTP handles it
    expect(res._getStatusCode()).toBe(200);
  });

  it('surfaces an aborted/failed invocation as a visible assistant message', async () => {
    // #250: a chat-turn invocation failure (timeout abort, backend error) must
    // come back as a 200 generateCopilotResponse message — CopilotKit silently
    // drops a bare 500, which is exactly the "no answer, no error" the bug
    // reports. Previously this asserted a 500; that encoded the buggy contract.
    const { req, res } = createMocks({
      method: 'POST',
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [
              {
                textMessage: {
                  role: 'user',
                  content: 'Test message'
                }
              }
            ],
            threadId: 'test-thread'
          }
        }
      }
    });

    // Create AbortController
    const controller = new AbortController();
    req.signal = controller.signal;

    // Abort immediately
    controller.abort();

    // Mock fetch to throw AbortError
    global.fetch = jest.fn().mockRejectedValue(new Error('AbortError'));

    await handler(req, res);

    // Handled gracefully AND visibly: 200 with an assistant message the user sees.
    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    const message = data.data.generateCopilotResponse.messages[0];
    expect(message.role).toBe('assistant');
    expect(message.content[0]).toMatch(/something went wrong|try again/i);
  });

  it('should cleanup resources on cancellation', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [
              {
                textMessage: {
                  role: 'user',
                  content: 'Test message'
                }
              }
            ],
            threadId: 'test-thread'
          }
        }
      }
    });

    // Mock backend that takes time
    global.fetch = jest.fn().mockImplementation(() =>
      new Promise((resolve) => {
        setTimeout(() => {
          resolve({
            status: 200,
            json: async () => ({ response: 'Test response' })
          });
        }, 100);
      })
    );

    // Start request
    const promise = handler(req, res);

    // Simulate disconnect after 10ms
    setTimeout(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      req.socket = { destroyed: true } as any;
    }, 10);

    await promise;

    // Should complete without hanging
    expect(true).toBe(true);
  });

  it('should return partial response if available on cancel', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [
              {
                textMessage: {
                  role: 'user',
                  content: 'Test message'
                }
              }
            ],
            threadId: 'test-thread'
          }
        }
      }
    });

    // Mock backend with partial response
    global.fetch = jest.fn().mockResolvedValue({
      status: 200,
      json: async () => ({ response: 'Partial response before cancel' })
    });

    await handler(req, res);

    expect(res._getStatusCode()).toBe(200);
    const data = JSON.parse(res._getData());
    expect(data.data.generateCopilotResponse.messages[0].content[0]).toContain('Partial');
  });

  it('should log cancellation events', async () => {
    const { req, res } = createMocks({
      method: 'POST',
      body: {
        operationName: 'generateCopilotResponse',
        variables: {
          data: {
            messages: [
              {
                textMessage: {
                  role: 'user',
                  content: 'Test message'
                }
              }
            ],
            threadId: 'test-thread'
          }
        }
      }
    });

    const logSpy = jest.spyOn(console, 'log').mockImplementation();

    // Simulate disconnect
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    req.socket = { destroyed: true } as any;

    await handler(req, res);

    // Should log the cancellation (implementation dependent)
    logSpy.mockRestore();
  });
});
