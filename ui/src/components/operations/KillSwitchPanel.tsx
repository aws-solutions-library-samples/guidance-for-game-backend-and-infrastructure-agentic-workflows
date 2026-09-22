import { useState } from 'react';
import type {
  CapabilityDiscovery,
  ControlRequest,
  ControlResponse,
  KillSwitchDocument,
} from '@/operations/schema';
import { buildControlRequest } from '@/operations/schema';
import { CONTROL_OUTCOME_LABELS, formatTimestamp } from '@/operations/labels';
import ConfirmDialog from './ConfirmDialog';

const CAP_ID = 'gamelift.capacity-adjustment' as const;

interface KillSwitchPanelProps {
  killSwitch: KillSwitchDocument;
  capabilities: CapabilityDiscovery[];
  onSubmit: (request: ControlRequest) => Promise<ControlResponse>;
}

interface PhaseState {
  prepare: boolean;
  dispatch: boolean;
  execute: boolean;
}

type SubmitStatus =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'done'; response: ControlResponse }
  | { kind: 'error' };

/**
 * The deployment / per-capability kill-switch control panel.
 *
 * Renders the deployment-wide master switch and the single capability's
 * prepare/dispatch/execute toggles, plus each capability's static and dynamic
 * gates. Applying a change requires an explicit confirmation dialog (focus
 * managed), sends a compare-and-set carrying the current config_version, and
 * announces the outcome in a polite live region (or an alert on failure).
 * Controls are disabled while a submission is in flight.
 *
 * A hidden or disabled control is never the authorization boundary — the
 * backend re-checks authority and the compare-and-set version independently.
 */
export default function KillSwitchPanel(props: KillSwitchPanelProps) {
  return <KillSwitchPanelState key={props.killSwitch.config_version} {...props} />;
}

function KillSwitchPanelState({
  killSwitch,
  capabilities,
  onSubmit,
}: KillSwitchPanelProps) {
  const current = killSwitch.capabilities[CAP_ID];
  const [operationsEnabled, setOperationsEnabled] = useState(killSwitch.operations_enabled);
  const [expectedConfigVersion, setExpectedConfigVersion] = useState(killSwitch.config_version);
  const [phases, setPhases] = useState<PhaseState>({
    prepare: current.prepare,
    dispatch: current.dispatch,
    execute: current.execute,
  });
  const [confirming, setConfirming] = useState(false);
  const [status, setStatus] = useState<SubmitStatus>({ kind: 'idle' });

  const busy = status.kind === 'submitting';

  const setPhase = (key: keyof PhaseState, value: boolean) => {
    setPhases((prev) => ({ ...prev, [key]: value }));
  };

  const doSubmit = async () => {
    setStatus({ kind: 'submitting' });
    const request = buildControlRequest(expectedConfigVersion, {
      operations_enabled: operationsEnabled,
      capabilities: {
        [CAP_ID]: { prepare: phases.prepare, dispatch: phases.dispatch, execute: phases.execute },
      },
    });
    try {
      const response = await onSubmit(request);
      if (response.outcome === 'version_conflict' || response.outcome === 'applied') {
        setExpectedConfigVersion(response.config_version);
      }
      setStatus({ kind: 'done', response });
    } catch {
      setStatus({ kind: 'error' });
    } finally {
      setConfirming(false);
    }
  };

  return (
    <section className="ga-ks" aria-label="Kill-switch controls">
      <header className="ga-ks-head">
        <h2 className="ga-ks-title">Kill switch</h2>
        <span className="ga-ks-version">
          Config version {killSwitch.config_version}
          {expectedConfigVersion !== killSwitch.config_version && (
            <> · retry against durable version {expectedConfigVersion}</>
          )}{' '}
          · valid until{' '}
          {formatTimestamp(killSwitch.not_after)}
        </span>
      </header>

      <div className="ga-ks-master">
        <label className="ga-ks-toggle">
          <span>Operations enabled (deployment-wide)</span>
          <button
            type="button"
            role="switch"
            aria-checked={operationsEnabled}
            aria-label="Operations enabled"
            className="ga-ks-switch"
            disabled={busy}
            onClick={() => setOperationsEnabled((v) => !v)}
          >
            <span className="ga-ks-knob" data-on={operationsEnabled} />
          </button>
        </label>
      </div>

      <div className="ga-ks-cap">
        <h3 className="ga-ks-cap-title">{CAP_ID}</h3>
        <div className="ga-ks-phases">
          {(['prepare', 'dispatch', 'execute'] as const).map((phase) => (
            <label key={phase} className="ga-ks-toggle">
              <span className="ga-ks-phase-label">{phase}</span>
              <button
                type="button"
                role="switch"
                aria-checked={phases[phase]}
                aria-label={`${CAP_ID} ${phase}`}
                className="ga-ks-switch"
                disabled={busy || !operationsEnabled}
                onClick={() => setPhase(phase, !phases[phase])}
              >
                <span className="ga-ks-knob" data-on={phases[phase]} />
              </button>
            </label>
          ))}
        </div>

        {capabilities.map((cap) => (
          <div key={cap.capability_id} className="ga-ks-gates-wrap">
            <h4 className="ga-ks-gates-title">Gates — {cap.capability_id}</h4>
            <ul className="ga-ks-gates" aria-label={`Gates for ${cap.capability_id}`}>
              {cap.gates.map((gate) => (
                <li key={gate.gate_id} className="ga-ks-gate">
                  <span
                    className={gate.satisfied ? 'ga-ks-gate-dot on' : 'ga-ks-gate-dot off'}
                    aria-hidden="true"
                  />
                  <span className="ga-ks-gate-id">{gate.gate_id}</span>
                  <span className={`ga-ks-gate-kind ga-ks-kind-${gate.kind}`}>{gate.kind}</span>
                  <span className="ga-ks-gate-state">
                    {gate.satisfied ? 'satisfied' : 'not satisfied'}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <div className="ga-ks-actions">
        <button
          type="button"
          className="ga-ks-apply"
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          Apply changes
        </button>
      </div>

      {/* Live regions: success/neutral outcomes are polite; failures assertive. */}
      <div className="ga-ks-live">
        {status.kind === 'done' && (
          <p role="status" aria-live="polite" className="ga-ks-outcome">
            {CONTROL_OUTCOME_LABELS[status.response.outcome]} (config version{' '}
            {status.response.config_version}).
          </p>
        )}
        {status.kind === 'error' && (
          <p role="alert" className="ga-ks-error">
            The change could not be applied. Please reload and try again.
          </p>
        )}
        {status.kind === 'submitting' && (
          <p role="status" aria-live="polite" className="ga-ks-muted">
            Applying change…
          </p>
        )}
      </div>

      {confirming && (
        <ConfirmDialog
          title="Apply kill-switch change?"
          confirmLabel="Confirm"
          cancelLabel="Cancel"
          danger
          busy={busy}
          onConfirm={doSubmit}
          onCancel={() => setConfirming(false)}
        >
          This updates the deployment kill switch (compare-and-set against config
          version {killSwitch.config_version}). The backend re-checks authority
          before any change takes effect.
        </ConfirmDialog>
      )}

      <style jsx>{styles}</style>
    </section>
  );
}

const styles = `
  .ga-ks { color: var(--ga-text); }
  .ga-ks-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  .ga-ks-title { margin: 0; font-size: 18px; }
  .ga-ks-version { color: var(--ga-text-subtle); font-size: 12px; }
  .ga-ks-master { margin: 16px 0; padding: 12px 16px; background: var(--ga-surface); border: 1px solid var(--ga-border); border-radius: 10px; }
  .ga-ks-cap { margin-top: 16px; }
  .ga-ks-cap-title { font-family: monospace; font-size: 14px; margin: 0 0 12px; }
  .ga-ks-phases { display: flex; flex-direction: column; gap: 8px; }
  .ga-ks-toggle { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .ga-ks-phase-label { text-transform: capitalize; }
  .ga-ks-switch {
    width: 46px; height: 26px; border-radius: 999px; border: 1px solid var(--ga-border);
    background: var(--ga-surface-muted); position: relative; cursor: pointer; padding: 0;
  }
  .ga-ks-switch[aria-checked="true"] { background: var(--ga-success-bg); border-color: var(--ga-success-border); }
  .ga-ks-switch:disabled { opacity: 0.5; cursor: not-allowed; }
  .ga-ks-switch:focus-visible { outline: 2px solid var(--ga-accent); outline-offset: 2px; }
  .ga-ks-knob {
    position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; border-radius: 50%;
    background: var(--ga-text-muted); transition: transform 0.15s;
  }
  .ga-ks-knob[data-on="true"] { transform: translateX(20px); background: var(--ga-success); }
  .ga-ks-gates-wrap { margin-top: 16px; }
  .ga-ks-gates-title { font-size: 13px; margin: 0 0 8px; color: var(--ga-text-muted); }
  .ga-ks-gates { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .ga-ks-gate { display: grid; grid-template-columns: auto 1fr auto auto; gap: 10px; align-items: center; font-size: 13px; }
  .ga-ks-gate-dot { width: 8px; height: 8px; border-radius: 50%; }
  .ga-ks-gate-dot.on { background: var(--ga-success); }
  .ga-ks-gate-dot.off { background: var(--ga-danger); }
  .ga-ks-gate-id { font-family: monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ga-ks-gate-kind { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--ga-border); }
  .ga-ks-kind-static { color: var(--ga-text-muted); background: var(--ga-surface-muted); }
  .ga-ks-kind-dynamic { color: var(--ga-accent); background: var(--ga-badge-bg); border-color: var(--ga-accent-border); }
  .ga-ks-gate-state { color: var(--ga-text-subtle); font-size: 12px; }
  .ga-ks-actions { margin-top: 20px; }
  .ga-ks-apply {
    background: var(--ga-accent); border: 1px solid var(--ga-accent-border-strong); color: #fff;
    border-radius: 8px; padding: 10px 20px; font-weight: 600; cursor: pointer;
  }
  .ga-ks-apply:disabled { opacity: 0.6; cursor: not-allowed; }
  .ga-ks-apply:focus-visible { outline: 2px solid var(--ga-accent); outline-offset: 2px; }
  .ga-ks-live { margin-top: 12px; min-height: 20px; }
  .ga-ks-outcome { color: var(--ga-success); }
  .ga-ks-error { color: var(--ga-danger); }
  .ga-ks-muted { color: var(--ga-text-muted); }
`;
