import type { OperationSummary } from '@/operations/schema';
import { STATE_LABELS, STATE_TONES, formatTimestamp } from '@/operations/labels';

export type LoadStatus = 'loading' | 'ready' | 'error';

interface OperationsListProps {
  status: LoadStatus;
  operations: OperationSummary[];
  onSelect: (operationId: string) => void;
  nextCursor?: string;
  onNext?: () => void;
  onRetry?: () => void;
}

/**
 * Accessible, paginated list of operation summaries. Each row shows only the
 * public-safe fields the projection carries (operation id, capability, state,
 * coarse timestamps). Rows are activatable buttons so keyboard and screen-reader
 * users can open the detail view.
 */
export default function OperationsList({
  status,
  operations,
  onSelect,
  nextCursor,
  onNext,
  onRetry,
}: OperationsListProps) {
  if (status === 'loading') {
    return (
      <div className="ga-ops-list" role="status" aria-live="polite">
        <span className="ga-ops-muted">Loading operations…</span>
        <style jsx>{styles}</style>
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className="ga-ops-list">
        <div role="alert" className="ga-ops-error">
          Could not load operations.
          {onRetry && (
            <button type="button" className="ga-ops-retry" onClick={onRetry}>
              Retry
            </button>
          )}
        </div>
        <style jsx>{styles}</style>
      </div>
    );
  }

  if (operations.length === 0) {
    return (
      <div className="ga-ops-list">
        <p className="ga-ops-muted">No operations to show.</p>
        <style jsx>{styles}</style>
      </div>
    );
  }

  return (
    <div className="ga-ops-list">
      <ul className="ga-ops-ul" aria-label="Operations">
        {operations.map((op) => (
          <li key={op.operation_id} className="ga-ops-li">
            <button
              type="button"
              className="ga-ops-row"
              onClick={() => onSelect(op.operation_id)}
              aria-label={`Operation ${op.operation_id}, ${STATE_LABELS[op.state]}`}
            >
              <span className="ga-ops-id">{op.operation_id}</span>
              <span className="ga-ops-cap">{op.capability_id}</span>
              <span className={`ga-ops-state ga-tone-${STATE_TONES[op.state]}`}>
                {STATE_LABELS[op.state]}
              </span>
              <span className="ga-ops-time">Updated {formatTimestamp(op.updated_at)}</span>
            </button>
          </li>
        ))}
      </ul>

      {nextCursor && onNext && (
        <div className="ga-ops-pager">
          <button type="button" className="ga-ops-next" onClick={onNext}>
            Next page
          </button>
        </div>
      )}
      <style jsx>{styles}</style>
    </div>
  );
}

const styles = `
  .ga-ops-list { color: var(--ga-text); }
  .ga-ops-muted { color: var(--ga-text-muted); }
  .ga-ops-ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .ga-ops-row {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(0, 2fr) auto minmax(0, 2fr);
    gap: 12px;
    align-items: center;
    width: 100%;
    text-align: left;
    padding: 12px 16px;
    background: var(--ga-surface);
    border: 1px solid var(--ga-border);
    border-radius: 10px;
    color: var(--ga-text);
    cursor: pointer;
  }
  .ga-ops-row:hover { background: var(--ga-control-hover-bg); border-color: var(--ga-accent-border); }
  .ga-ops-row:focus-visible { outline: 2px solid var(--ga-accent); outline-offset: 2px; }
  .ga-ops-id { font-family: monospace; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ga-ops-cap { color: var(--ga-text-muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ga-ops-time { color: var(--ga-text-subtle); font-size: 12px; text-align: right; }
  .ga-ops-state {
    justify-self: start;
    padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
    border: 1px solid var(--ga-border);
  }
  .ga-tone-success { color: var(--ga-success); background: var(--ga-success-bg); border-color: var(--ga-success-border); }
  .ga-tone-danger { color: var(--ga-danger); background: var(--ga-danger-bg); border-color: var(--ga-danger-border); }
  .ga-tone-progress { color: var(--ga-warning); background: var(--ga-warning-bg); border-color: var(--ga-warning-border); }
  .ga-tone-neutral { color: var(--ga-text); background: var(--ga-badge-bg); }
  .ga-tone-muted { color: var(--ga-text-muted); background: var(--ga-surface-muted); }
  .ga-ops-error {
    color: var(--ga-danger); background: var(--ga-danger-bg);
    border: 1px solid var(--ga-danger-border); border-radius: 10px; padding: 16px;
    display: flex; align-items: center; gap: 12px;
  }
  .ga-ops-retry, .ga-ops-next {
    background: var(--ga-control-hover-bg); border: 1px solid var(--ga-accent-border);
    color: var(--ga-accent); border-radius: 8px; padding: 8px 16px; font-weight: 500; cursor: pointer;
  }
  .ga-ops-retry:focus-visible, .ga-ops-next:focus-visible { outline: 2px solid var(--ga-accent); outline-offset: 2px; }
  .ga-ops-pager { margin-top: 16px; display: flex; justify-content: center; }
`;
