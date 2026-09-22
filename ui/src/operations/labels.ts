/**
 * Human-readable, public-safe labels for the frozen operator enums, plus small
 * formatting helpers. Kept separate from components so a reskin reuses them and
 * so they are unit-testable in isolation.
 */
import type {
  ControlOutcome,
  EvidenceCategory,
  OperationState,
  PhaseName,
  PhaseStatus,
  VisibilityOutcome,
} from '@/operations/schema';

export const STATE_LABELS: Record<OperationState, string> = {
  prepared: 'Prepared',
  pending_approval: 'Pending approval',
  approved: 'Approved',
  dispatched: 'Dispatched',
  executing: 'Executing',
  retry_pending: 'Retry pending',
  succeeded: 'Succeeded',
  failed: 'Failed',
  rejected: 'Rejected',
  cancelled: 'Cancelled',
  expired: 'Expired',
};

/** A coarse tone used only for token-driven styling; never security-bearing. */
export type Tone = 'neutral' | 'progress' | 'success' | 'danger' | 'muted';

export const STATE_TONES: Record<OperationState, Tone> = {
  prepared: 'neutral',
  pending_approval: 'progress',
  approved: 'progress',
  dispatched: 'progress',
  executing: 'progress',
  retry_pending: 'progress',
  succeeded: 'success',
  failed: 'danger',
  rejected: 'danger',
  cancelled: 'muted',
  expired: 'muted',
};

export const PHASE_LABELS: Record<PhaseName, string> = {
  prepare: 'Proposal',
  approve: 'Approval',
  dispatch: 'Dispatch',
  execute: 'Execution',
  verify: 'Verification',
  rollback: 'Rollback',
};

/**
 * The lifecycle phases the detail timeline distinguishes, in canonical order.
 * "Authorization" is surfaced from the authorization evidence category rather
 * than a phase, so the timeline can label it distinctly from approval.
 */
export const PHASE_ORDER: PhaseName[] = [
  'prepare',
  'approve',
  'dispatch',
  'execute',
  'verify',
  'rollback',
];

export const PHASE_STATUS_LABELS: Record<PhaseStatus, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  succeeded: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped',
};

export const PHASE_STATUS_TONES: Record<PhaseStatus, Tone> = {
  not_started: 'muted',
  in_progress: 'progress',
  succeeded: 'success',
  failed: 'danger',
  skipped: 'muted',
};

export const EVIDENCE_CATEGORY_LABELS: Record<EvidenceCategory, string> = {
  authorization: 'Authorization',
  approval: 'Approval',
  dispatch: 'Dispatch',
  verification: 'Verification',
  rollback: 'Rollback',
  state_change: 'State change',
};

export const VISIBILITY_OUTCOME_LABELS: Record<VisibilityOutcome, string> = {
  not_applicable: 'Not applicable',
  pending: 'Pending',
  succeeded: 'Succeeded',
  failed: 'Failed',
};

export const CONTROL_OUTCOME_LABELS: Record<ControlOutcome, string> = {
  applied: 'Change applied',
  version_conflict: 'Version conflict — reload and retry',
  denied: 'Change denied',
};

/**
 * Format an ISO timestamp for display. Falls back to the raw string if it is
 * unparseable (the schema guard already validated it, so this is belt-and-braces).
 */
export function formatTimestamp(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}
