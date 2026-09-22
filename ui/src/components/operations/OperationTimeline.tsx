import type { OperationDetail } from '@/operations/schema';
import {
  EVIDENCE_CATEGORY_LABELS,
  PHASE_LABELS,
  PHASE_STATUS_LABELS,
  PHASE_STATUS_TONES,
  STATE_LABELS,
  STATE_TONES,
  VISIBILITY_OUTCOME_LABELS,
  formatTimestamp,
} from '@/operations/labels';

interface OperationTimelineProps {
  detail: OperationDetail;
}

/**
 * The operation detail view: a lifecycle timeline that distinguishes the
 * proposal, authorization/approval, dispatch, execution, verification, rollback,
 * and terminal facets, plus a bounded set of provider-free evidence summaries.
 * Renders only public-safe projection fields; there is no raw payload, ARN,
 * account, or fleet id anywhere in the projection.
 */
export default function OperationTimeline({ detail }: OperationTimelineProps) {
  return (
    <section className="ga-op-detail" aria-label={`Operation ${detail.operation_id}`}>
      <header className="ga-op-head">
        <div>
          <span className="ga-op-id">{detail.operation_id}</span>
          <span className="ga-op-cap">{detail.capability_id}</span>
        </div>
        <span className={`ga-op-state ga-tone-${STATE_TONES[detail.state]}`}>
          {STATE_LABELS[detail.state]}
        </span>
      </header>

      <dl className="ga-op-meta">
        <div>
          <dt>Created</dt>
          <dd>{formatTimestamp(detail.created_at)}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{formatTimestamp(detail.updated_at)}</dd>
        </div>
      </dl>

      <h3 className="ga-op-h3">Lifecycle</h3>
      <ol className="ga-op-timeline" aria-label="Lifecycle timeline">
        {detail.phases.map((phase, i) => (
          <li key={`${phase.phase}-${i}`} className="ga-op-phase">
            <span className={`ga-op-dot ga-tone-${PHASE_STATUS_TONES[phase.status]}`} aria-hidden="true" />
            <span className="ga-op-phase-name">{PHASE_LABELS[phase.phase]}</span>
            <span className={`ga-op-phase-status ga-tone-${PHASE_STATUS_TONES[phase.status]}`}>
              {PHASE_STATUS_LABELS[phase.status]}
            </span>
            {phase.occurred_at && (
              <span className="ga-op-phase-time">{formatTimestamp(phase.occurred_at)}</span>
            )}
          </li>
        ))}
      </ol>

      <div className="ga-op-visibility">
        <div className="ga-op-vis-card">
          <h4>Verification</h4>
          <p>
            {detail.verification.applicable ? 'Applicable' : 'Not applicable'} —{' '}
            {VISIBILITY_OUTCOME_LABELS[detail.verification.outcome]}
          </p>
        </div>
        <div className="ga-op-vis-card">
          <h4>Rollback</h4>
          <p>
            {detail.rollback.applicable ? 'Applicable' : 'Not applicable'} —{' '}
            {VISIBILITY_OUTCOME_LABELS[detail.rollback.outcome]}
          </p>
        </div>
      </div>

      <h3 className="ga-op-h3">Evidence</h3>
      {detail.evidence.length === 0 ? (
        <p className="ga-op-muted">No evidence recorded.</p>
      ) : (
        <ul className="ga-op-evidence" aria-label="Evidence">
          {detail.evidence.map((e, i) => (
            <li key={`${e.category}-${i}`} className="ga-op-evidence-item">
              <span className="ga-op-evidence-cat">{EVIDENCE_CATEGORY_LABELS[e.category]}</span>
              <span className="ga-op-evidence-summary">{e.summary}</span>
              {e.recorded_at && (
                <span className="ga-op-evidence-time">{formatTimestamp(e.recorded_at)}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <style jsx>{styles}</style>
    </section>
  );
}

const styles = `
  .ga-op-detail { color: var(--ga-text); }
  .ga-op-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  .ga-op-id { font-family: monospace; font-weight: 600; margin-right: 12px; }
  .ga-op-cap { color: var(--ga-text-muted); font-size: 14px; }
  .ga-op-state, .ga-op-phase-status {
    padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--ga-border);
  }
  .ga-tone-success { color: var(--ga-success); background: var(--ga-success-bg); border-color: var(--ga-success-border); }
  .ga-tone-danger { color: var(--ga-danger); background: var(--ga-danger-bg); border-color: var(--ga-danger-border); }
  .ga-tone-progress { color: var(--ga-warning); background: var(--ga-warning-bg); border-color: var(--ga-warning-border); }
  .ga-tone-neutral { color: var(--ga-text); background: var(--ga-badge-bg); }
  .ga-tone-muted { color: var(--ga-text-muted); background: var(--ga-surface-muted); }
  .ga-op-meta { display: flex; gap: 32px; margin: 16px 0; }
  .ga-op-meta dt { color: var(--ga-text-subtle); font-size: 12px; }
  .ga-op-meta dd { margin: 2px 0 0 0; font-size: 14px; }
  .ga-op-h3 { margin: 24px 0 12px; font-size: 15px; }
  .ga-op-timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
  .ga-op-phase { display: grid; grid-template-columns: auto 1fr auto auto; gap: 12px; align-items: center; }
  .ga-op-dot { width: 10px; height: 10px; border-radius: 50%; }
  .ga-op-dot.ga-tone-success { background: var(--ga-success); }
  .ga-op-dot.ga-tone-danger { background: var(--ga-danger); }
  .ga-op-dot.ga-tone-progress { background: var(--ga-warning); }
  .ga-op-dot.ga-tone-muted { background: var(--ga-text-muted); }
  .ga-op-phase-name { font-weight: 500; }
  .ga-op-phase-time, .ga-op-evidence-time { color: var(--ga-text-subtle); font-size: 12px; }
  .ga-op-visibility { display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }
  .ga-op-vis-card { flex: 1 1 200px; background: var(--ga-surface); border: 1px solid var(--ga-border); border-radius: 10px; padding: 12px 16px; }
  .ga-op-vis-card h4 { margin: 0 0 6px; font-size: 13px; }
  .ga-op-vis-card p { margin: 0; color: var(--ga-text-muted); font-size: 14px; }
  .ga-op-evidence { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .ga-op-evidence-item {
    display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: baseline;
    background: var(--ga-surface); border: 1px solid var(--ga-border); border-radius: 10px; padding: 10px 16px;
  }
  .ga-op-evidence-cat { font-weight: 600; font-size: 13px; }
  .ga-op-evidence-summary { color: var(--ga-text-muted); font-size: 14px; }
  .ga-op-muted { color: var(--ga-text-muted); }
`;
