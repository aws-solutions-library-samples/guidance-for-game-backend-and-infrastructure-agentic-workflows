/**
 * Defense-in-depth tests for the E4 operations upstream proxy plumbing (#416).
 *
 * These cover the transport-boundary controls that sit *below* the schema
 * guards, so a hostile or misbehaving upstream cannot be used to exfiltrate the
 * forwarded credential, smuggle a redirect, or exhaust memory before the body
 * is ever parsed:
 *   - fetch is issued with redirect:'manual' and any 3xx maps to a bounded 502;
 *   - the server-only base URL must be HTTPS outside the explicit local-dev
 *     bypass;
 *   - a Content-Length over the ceiling, or a streamed body that exceeds the
 *     byte ceiling, is refused as a bounded 502 before JSON parsing;
 *   - malformed JSON maps to a bounded 502.
 * In every failure path the sanitized error body carries no upstream detail and
 * the request is not retried against a redirect target with the credential.
 */
import {
  MAX_UPSTREAM_BYTES,
  UpstreamProxyError,
  readBoundedJson,
  requireSecureBaseUrl,
  upstreamGet,
  upstreamPost,
} from '@/operations/proxy';
import { BaseUrlError } from '@/operations/proxyAuth';

const ENV = { ...process.env };

afterEach(() => {
  process.env = { ...ENV };
  jest.restoreAllMocks();
});

/**
 * Build a minimal fetch Response stand-in with a ReadableStream body so we can
 * exercise the streamed byte-ceiling path deterministically.
 */
function streamResponse(
  status: number,
  chunks: Uint8Array[],
  headers: Record<string, string> = {},
): Response {
  let i = 0;
  const body = {
    getReader() {
      return {
        read: async () => {
          if (i < chunks.length) {
            const value = chunks[i++];
            return { done: false, value };
          }
          return { done: true, value: undefined };
        },
        cancel: async () => undefined,
        releaseLock: () => undefined,
      };
    },
  };
  const hdr = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    redirected: false,
    headers: { get: (name: string) => hdr.get(name.toLowerCase()) ?? null },
    body,
  } as unknown as Response;
}

function jsonBytes(value: unknown): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(value));
}

describe('requireSecureBaseUrl', () => {
  it('accepts an https base URL', () => {
    process.env.NODE_ENV = 'production';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    expect(requireSecureBaseUrl('https://api.example.com')).toBe('https://api.example.com');
  });

  it('rejects a plaintext http base URL outside the local-dev bypass', () => {
    process.env.NODE_ENV = 'production';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    expect(() => requireSecureBaseUrl('http://api.example.com')).toThrow(BaseUrlError);
  });

  it('allows a plaintext http base URL only in the explicit local-dev bypass', () => {
    process.env.NODE_ENV = 'development';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'true';
    expect(requireSecureBaseUrl('http://localhost:8080')).toBe('http://localhost:8080');
  });
});

describe('readBoundedJson', () => {
  it('parses a small, valid JSON body', async () => {
    const resp = streamResponse(200, [jsonBytes({ ok: true })]);
    await expect(readBoundedJson(resp)).resolves.toEqual({ ok: true });
  });

  it('refuses a body whose Content-Length exceeds the ceiling before reading', async () => {
    const resp = streamResponse(200, [jsonBytes({ ok: true })], {
      'content-length': String(MAX_UPSTREAM_BYTES + 1),
    });
    await expect(readBoundedJson(resp)).rejects.toBeInstanceOf(UpstreamProxyError);
  });

  it('refuses a streamed body that exceeds the ceiling even without Content-Length', async () => {
    const oversized = new Uint8Array(MAX_UPSTREAM_BYTES + 10);
    const resp = streamResponse(200, [oversized]);
    await expect(readBoundedJson(resp)).rejects.toBeInstanceOf(UpstreamProxyError);
  });

  it('maps malformed JSON to an UpstreamProxyError', async () => {
    const resp = streamResponse(200, [new TextEncoder().encode('{ not json')]);
    await expect(readBoundedJson(resp)).rejects.toBeInstanceOf(UpstreamProxyError);
  });
});

describe('upstreamGet / upstreamPost transport hardening', () => {
  beforeEach(() => {
    process.env.NODE_ENV = 'production';
    process.env.NEXT_PUBLIC_SKIP_AUTH = 'false';
    process.env.GBAW_OPERATIONS_API_BASE_URL = 'https://api.example.com';
  });

  it('issues the request with redirect set to manual', async () => {
    const fetchMock = jest.fn(async () => streamResponse(200, [jsonBytes({ ok: true })]));
    global.fetch = fetchMock as unknown as typeof fetch;
    await upstreamGet('tok', '/operations');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.redirect).toBe('manual');
  });

  it('maps a 3xx redirect response to a bounded 502-class error without following it', async () => {
    const fetchMock = jest.fn(async () =>
      streamResponse(302, [], { location: 'https://evil.example.com/steal' }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;
    const result = await upstreamGet('tok', '/operations');
    expect(result.status).toBe(502);
    // The credential is forwarded exactly once — never replayed to the redirect target.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('maps an oversize upstream body to a bounded 502', async () => {
    const oversized = new Uint8Array(MAX_UPSTREAM_BYTES + 10);
    const fetchMock = jest.fn(async () => streamResponse(200, [oversized]));
    global.fetch = fetchMock as unknown as typeof fetch;
    const result = await upstreamGet('tok', '/operations');
    expect(result.status).toBe(502);
  });

  it('maps a malformed upstream body to a bounded 502', async () => {
    const fetchMock = jest.fn(async () =>
      streamResponse(200, [new TextEncoder().encode('{ not json')]),
    );
    global.fetch = fetchMock as unknown as typeof fetch;
    const result = await upstreamPost('tok', '/operations/control', { a: 1 });
    expect(result.status).toBe(502);
  });

  it('refuses to call a plaintext http base URL in production (no fetch, bounded 502)', async () => {
    process.env.GBAW_OPERATIONS_API_BASE_URL = 'http://api.example.com';
    const fetchMock = jest.fn(async () => streamResponse(200, [jsonBytes({ ok: true })]));
    global.fetch = fetchMock as unknown as typeof fetch;
    const result = await upstreamGet('tok', '/operations');
    expect(result.status).toBe(502);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
