# ADOT Exporter Credentials at Cold Start (Telemetry Limitation)

This guidance uses the AgentCore-managed AWS Distro for OpenTelemetry (ADOT) for
runtime traces and logs. Under `agentcore launch`, the platform wraps the
entrypoint with `opentelemetry-instrument` and auto-configures the OTLP
exporters, their endpoints, and the SigV4 credential source. The application
code sets **no** exporter, endpoint, or credential configuration (see the ADOT
section of `backend/src/config/settings.py`) — it delegates entirely to the
platform.

## Known limitation (platform/package-owned)

Immediately after a **cold start** (a new runtime instance), the ADOT OTLP
exporter's background export thread can fire before the runtime workload
credential provider is fully seeded. The export request is then signed with no
credentials and the telemetry endpoint rejects it. This surfaces in the
runtime's `otel-rt-logs` stream as, for example:

```text
Failed to load AWS Credentials
Failed to export span batch code: 403, reason: Forbidden
```

(The log-batch variant is `... code: 403 ... Missing Authentication Token`.)

Characteristics of this **transient** behavior:

- It is **bounded to cold start**: each occurrence is adjacent to a new runtime
  instance appearing, and is more likely during concurrent scale-out.
- The **application is unaffected**: the app's own AWS SDK calls (Bedrock,
  GameLift, EKS, Cost Explorer, CloudWatch, DynamoDB) succeed in the same window,
  and the runtime logs show `Found credentials from IAM Role: execution_role`.
- Telemetry **recovers on warm requests**: subsequent exports succeed and traces
  continue to arrive in X-Ray Transaction Search (`aws/spans`).
- Only the affected in-flight batch may be dropped.

This is distinct from the missing-`aws/spans`-log-group failure
(`400 ResourceNotFoundException`), which the deployment already works around in
`scripts/infrastructure/setup-account-observability.sh` and `scripts/deploy.sh`.

## Ownership and upstream tracking

Root cause is **platform/package-owned** — the AgentCore-managed ADOT exporter
credential lifecycle at cold start, not this repository's configuration or IAM.
The runtime execution role already grants the telemetry permissions the exporter
needs (`logs:PutLogEvents` on the runtime and spans log groups;
`xray:PutTraceSegments` / `xray:PutTelemetryRecords`), and those permissions are
exercised successfully outside the cold-start window.

Track the exporter credential-availability timing with the AWS Distro for
OpenTelemetry for Python and the Bedrock AgentCore starter toolkit. The related
trace-delivery setup gap is tracked upstream at
`aws/bedrock-agentcore-starter-toolkit` issue 457 (a *different*, already
worked-around failure mode).

## Detection (does not suppress)

A read-only detection check surfaces these failures instead of hiding them:

- `backend/src/utils/adot_exporter_auth_detector.py` — pure classification logic
  (unit-tested in `backend/tests/unit/test_adot_exporter_auth_detector_unit.py`).
  It matches the exporter auth signatures, **excludes** the unrelated
  `400 / ResourceNotFound` case, clusters near-simultaneous lines into incidents,
  and classifies the result as:
  - `clean` — no exporter auth failures (PASS);
  - `transient_cold_start` — a bounded number of incidents, each adjacent to an
    instance transition (WARN, non-blocking — the known behavior above);
  - `persistent` — failures not bounded to cold start, or above the transient
    budget (FAIL — investigate as a real regression, e.g. a broken IAM policy or
    exporter misconfiguration).
- `scripts/infrastructure/check-exporter-auth.sh` — read-only wrapper that
  fetches the runtime logs and runs the classifier. Run it directly:

  ```bash
  AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
    scripts/infrastructure/check-exporter-auth.sh --runtime-id <runtime-id> --lookback-hours 24
  ```

  It is also invoked by `validate-deployment.sh` as a non-blocking observability
  check after a deployment.

## Minimal, public-safe reproduction

The transient credential gap is a startup-timing race in the exporter's SigV4
signing, reproducible in isolation without AgentCore. The following synthetic
example (no account IDs, ARNs, endpoints, or real logs) shows an OTLP HTTP
exporter attempting to sign an export before a credential provider is ready, and
the resulting `403`:

```python
# Synthetic reproduction of the cold-start credential-availability race.
# The exporter's background thread signs and sends before creds are seeded.
import threading
import time

class LazyCredentialProvider:
    """Credentials become available only after `seed_delay_s` (simulated cold start)."""
    def __init__(self, seed_delay_s: float):
        self._ready_at = time.monotonic() + seed_delay_s

    def get_credentials(self):
        if time.monotonic() < self._ready_at:
            return None  # not seeded yet -> request is signed with no credentials
        return {"access_key": "AKIAEXAMPLE", "secret_key": "example-secret"}  # synthetic

def sigv4_sign_and_send(provider):
    creds = provider.get_credentials()
    if creds is None:
        # Endpoint rejects an unsigned request during the gap.
        return 403, "Forbidden"  # ~ "Missing Authentication Token"
    return 200, "OK"

def export_batch(provider, results):
    results.append(sigv4_sign_and_send(provider))

if __name__ == "__main__":
    # Exporter thread fires immediately; credentials seed 200ms later.
    provider = LazyCredentialProvider(seed_delay_s=0.2)
    results = []
    early = threading.Thread(target=export_batch, args=(provider, results))
    early.start(); early.join()
    print("cold-start export:", results[-1])   # -> (403, 'Forbidden')

    time.sleep(0.25)                            # warm: creds now seeded
    export_batch(provider, results)
    print("warm export:      ", results[-1])   # -> (200, 'OK')
```

Expected output:

```text
cold-start export: (403, 'Forbidden')
warm export:       (200, 'OK')
```

This models the observed behavior: an export during the credential-seeding gap
is rejected with `403`, while the next export after seeding succeeds. The fix
belongs in the exporter/platform credential lifecycle (wait for a ready
provider before the first export, or retry the signed request once credentials
resolve); the application should not disable or suppress the exporter.

## Rollback / disable path

There is no repository-owned exporter configuration to roll back — the exporter
is platform-managed. If a future repository change to observability wiring (for
example, the detection check or the account-observability setup) regresses,
revert that focused change; AgentCore-managed baseline observability continues to
operate while the upstream timing behavior is tracked.
