import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import Navigation from '@/components/Navigation';
import OperationsList, { type LoadStatus } from '@/components/operations/OperationsList';
import OperationTimeline from '@/components/operations/OperationTimeline';
import KillSwitchPanel from '@/components/operations/KillSwitchPanel';
import {
  OperationsClientError,
  fetchCapabilities,
  fetchKillSwitch,
  fetchOperations,
  fetchOperationDetail,
  submitControl,
} from '@/operations/client';
import type {
  CapabilityDiscoveryDocument,
  ControlRequest,
  KillSwitchDocument,
  OperationDetail,
  OperationsListResponse,
} from '@/operations/schema';
import { logError } from '@/utils/logger';

/**
 * The operator console page. It composes the capability-gated surface:
 * a paginated operation list, an on-demand detail timeline, and the
 * deployment/per-capability kill-switch panel.
 *
 * Access is enforced server-side (the proxy routes require the admin group and
 * re-check gates); this page renders a clear access-denied message on a 403
 * rather than exposing anything.
 */
export default function OperationsPage() {
  const [capabilities, setCapabilities] = useState<CapabilityDiscoveryDocument | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitchDocument | null>(null);
  const [list, setList] = useState<OperationsListResponse | null>(null);
  const [listStatus, setListStatus] = useState<LoadStatus>('loading');
  const [detail, setDetail] = useState<OperationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  const loadList = useCallback(async () => {
    setListStatus('loading');
    try {
      setList(await fetchOperations({ pageSize: 25 }));
      setListStatus('ready');
    } catch (error) {
      setListStatus('error');
      logError('Failed to load operations', error instanceof Error ? error : undefined);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [caps, ks] = await Promise.all([fetchCapabilities(), fetchKillSwitch()]);
        if (cancelled) return;
        setCapabilities(caps);
        setKillSwitch(ks);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof OperationsClientError && error.status === 403) {
          setAccessDenied(true);
          return;
        }
        setFatal('The operations console could not be loaded.');
        logError('Failed to load operator console', error instanceof Error ? error : undefined);
        return;
      }
      if (!cancelled) await loadList();
    })();
    return () => {
      cancelled = true;
    };
  }, [loadList]);

  const onSelect = useCallback(async (operationId: string) => {
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await fetchOperationDetail(operationId));
    } catch (error) {
      logError('Failed to load operation detail', error instanceof Error ? error : undefined);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const onSubmitControl = useCallback(async (request: ControlRequest) => {
    const response = await submitControl(request);
    // Refresh the kill-switch document so the panel reflects the new version.
    try {
      setKillSwitch(await fetchKillSwitch());
    } catch {
      /* non-fatal: the response already carries the new version */
    }
    return response;
  }, []);

  return (
    <>
      <Head>
        <title>Operations - Game Agent</title>
      </Head>
      <div className="ga-layout">
        <Navigation pageTitle="Operations" showBackButton={true} />
        <main className="ga-ops-main">
          <div className="ga-ops-container">
            {accessDenied ? (
              <div role="alert" className="ga-ops-denied">
                You do not have operator access. Operations controls require the admin group.
              </div>
            ) : fatal ? (
              <div role="alert" className="ga-ops-denied">
                {fatal}
              </div>
            ) : (
              <>
                <section aria-label="Operations" className="ga-ops-section">
                  <h2 className="ga-ops-h2">Operations</h2>
                  <OperationsList
                    status={listStatus}
                    operations={list?.operations ?? []}
                    onSelect={onSelect}
                    nextCursor={list?.next_cursor}
                    onNext={loadList}
                    onRetry={loadList}
                  />
                </section>

                <section aria-label="Operation detail" className="ga-ops-section">
                  <h2 className="ga-ops-h2">Detail</h2>
                  {detailLoading ? (
                    <p role="status" aria-live="polite" className="ga-ops-muted">
                      Loading operation…
                    </p>
                  ) : detail ? (
                    <OperationTimeline detail={detail} />
                  ) : (
                    <p className="ga-ops-muted">Select an operation to view its timeline.</p>
                  )}
                </section>

                <section className="ga-ops-section">
                  {killSwitch && capabilities ? (
                    <KillSwitchPanel
                      killSwitch={killSwitch}
                      capabilities={capabilities.capabilities}
                      onSubmit={onSubmitControl}
                    />
                  ) : (
                    <p role="status" aria-live="polite" className="ga-ops-muted">
                      Loading controls…
                    </p>
                  )}
                </section>
              </>
            )}
          </div>
        </main>
      </div>
      <style jsx>{`
        .ga-ops-main {
          max-width: 1000px;
          margin: 0 auto;
          padding: 32px 24px 64px;
        }
        .ga-ops-container {
          display: flex;
          flex-direction: column;
          gap: 32px;
        }
        .ga-ops-section {
          background: var(--ga-surface-elevated);
          border: 1px solid var(--ga-border);
          border-radius: 14px;
          padding: 24px;
        }
        .ga-ops-h2 {
          margin: 0 0 16px;
          font-size: 18px;
          color: var(--ga-text);
        }
        .ga-ops-muted {
          color: var(--ga-text-muted);
        }
        .ga-ops-denied {
          background: var(--ga-danger-bg);
          border: 1px solid var(--ga-danger-border);
          color: var(--ga-danger);
          border-radius: 12px;
          padding: 24px;
          font-size: 15px;
        }
      `}</style>
    </>
  );
}
