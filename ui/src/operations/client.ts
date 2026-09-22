/**
 * Browser-side client for the operator UI. It calls the same-origin proxy
 * routes (never the backend directly) and validates every response with the
 * shared schema guards, so components only ever see already-validated,
 * browser-safe data. Tokens live in HttpOnly cookies and are added by the
 * browser automatically; this client never handles them.
 */
import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import {
  parseCancelResponse,
  parseCapabilityDiscovery,
  parseControlResponse,
  parseKillSwitch,
  parseOperationDetail,
  parseOperationsListResponse,
  type CancelResponse,
  type CapabilityDiscoveryDocument,
  type ControlRequest,
  type ControlResponse,
  type KillSwitchDocument,
  type OperationDetail,
  type OperationsListResponse,
  type OperationState,
} from '@/operations/schema';

export class OperationsClientError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'OperationsClientError';
    this.status = status;
  }
}

async function getJson(url: string): Promise<unknown> {
  const res = await fetchWithTimeout(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    throw new OperationsClientError(res.status, `Request failed (${res.status})`);
  }
  return res.json();
}

export async function fetchCapabilities(): Promise<CapabilityDiscoveryDocument> {
  return parseCapabilityDiscovery(await getJson('/api/operations/capabilities'));
}

export interface ListParams {
  pageSize?: number;
  cursor?: string;
  capabilityId?: string;
  states?: OperationState[];
}

export async function fetchOperations(params: ListParams = {}): Promise<OperationsListResponse> {
  const query = new URLSearchParams();
  if (params.pageSize !== undefined) query.set('page_size', String(params.pageSize));
  if (params.cursor) query.set('cursor', params.cursor);
  if (params.capabilityId) query.set('capability_id', params.capabilityId);
  if (params.states && params.states.length > 0) query.set('states', params.states.join(','));
  const qs = query.toString();
  return parseOperationsListResponse(await getJson(`/api/operations${qs ? `?${qs}` : ''}`));
}

export async function fetchOperationDetail(operationId: string): Promise<OperationDetail> {
  return parseOperationDetail(
    await getJson(`/api/operations/${encodeURIComponent(operationId)}`),
  );
}

export async function fetchKillSwitch(): Promise<KillSwitchDocument> {
  return parseKillSwitch(await getJson('/api/operations/kill-switch'));
}

export async function submitControl(request: ControlRequest): Promise<ControlResponse> {
  const res = await fetchWithTimeout('/api/operations/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    throw new OperationsClientError(res.status, `Control request failed (${res.status})`);
  }
  return parseControlResponse(await res.json());
}

/**
 * Request cancellation of a pre-dispatch operation via the same-origin E2
 * action proxy route. The operation id is a path parameter; there is no body.
 * The proxy enforces admin + CSRF and forwards the verified access token; this
 * client never handles credentials. Callers should re-fetch the detail after a
 * success to refresh the timeline.
 */
export async function cancelOperation(operationId: string): Promise<CancelResponse> {
  const res = await fetchWithTimeout(
    `/api/operations/${encodeURIComponent(operationId)}/cancel`,
    { method: 'POST', headers: { Accept: 'application/json' } },
  );
  if (!res.ok) {
    throw new OperationsClientError(res.status, `Cancel request failed (${res.status})`);
  }
  return parseCancelResponse(await res.json());
}
