import type { NextApiRequest, NextApiResponse } from 'next';
import {
  MAX_PAGE_SIZE,
  OPERATION_STATES,
  OPERATIONS_ROUTES,
  parseOperationsListResponse,
  type OperationState,
} from '@/operations/schema';
import { authorizeOperator, upstreamGet, validateAndSend } from '@/operations/proxy';

const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const CURSOR_PATTERN = /^[A-Za-z0-9_-]+$/;
const CURSOR_MAX_LENGTH = 512;

function firstValue(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

/**
 * GET /api/operations
 * Proxies the frozen E4 route `GET /operations`.
 *
 * Untrusted query params (page_size, cursor, filter.capability_id,
 * filter.states) are validated against the frozen bounds here before the
 * backend is called, so a malformed or oversized request never reaches it.
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const params = new URLSearchParams();

  const pageSizeRaw = firstValue(req.query.page_size);
  if (pageSizeRaw !== undefined) {
    const pageSize = Number(pageSizeRaw);
    if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > MAX_PAGE_SIZE) {
      return res.status(400).json({ error: `page_size must be an integer in [1, ${MAX_PAGE_SIZE}]` });
    }
    params.set('page_size', String(pageSize));
  }

  const cursor = firstValue(req.query.cursor);
  if (cursor !== undefined) {
    if (!CURSOR_PATTERN.test(cursor) || cursor.length > CURSOR_MAX_LENGTH) {
      return res.status(400).json({ error: 'Invalid cursor' });
    }
    params.set('cursor', cursor);
  }

  const capabilityId = firstValue(req.query.capability_id);
  if (capabilityId !== undefined) {
    if (!IDENTIFIER_PATTERN.test(capabilityId) || capabilityId.length > 128) {
      return res.status(400).json({ error: 'Invalid capability_id filter' });
    }
    params.set('capability_id', capabilityId);
  }

  const statesRaw = firstValue(req.query.states);
  if (statesRaw !== undefined && statesRaw !== '') {
    const states = statesRaw.split(',').map((s) => s.trim());
    if (states.length < 1 || states.length > 11 || new Set(states).size !== states.length) {
      return res.status(400).json({ error: 'Invalid states filter' });
    }
    for (const s of states) {
      if (!(OPERATION_STATES as readonly string[]).includes(s)) {
        return res.status(400).json({ error: `Unknown operation state: ${s}` });
      }
    }
    params.set('states', (states as OperationState[]).join(','));
  }

  const accessToken = await authorizeOperator(req, res);
  if (accessToken === null) return;

  const query = params.toString();
  const path = query ? `${OPERATIONS_ROUTES.list}?${query}` : OPERATIONS_ROUTES.list;
  const upstream = await upstreamGet(accessToken, path);
  validateAndSend(res, upstream, parseOperationsListResponse);
}
