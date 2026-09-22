/**
 * TypeScript schema guards for the frozen E4 operations control-plane contracts
 * (issue #416).
 *
 * These guards are the UI's contract boundary: every response that crosses from
 * the backend into the browser is validated here against the frozen v1 shapes
 * before any component sees it. They are intentionally dependency-free (no ajv)
 * so they add no bundle weight and can run in both the Node API-proxy layer and
 * the browser.
 *
 * Two invariants they enforce beyond structural shape:
 *  1. Frozen enums / patterns / bounds from the v1 schemas.
 *  2. A hard denylist of sensitive fields (email, display_name, token, arn,
 *     account_id, fleet_id, raw provider payloads). The projections are
 *     public-safe by contract; if any such field ever appears we fail closed
 *     rather than render it.
 */

// ---------------------------------------------------------------------------
// Frozen constants (must match backend/src/operations/contracts/control_plane.py
// and the v1 JSON schemas).
// ---------------------------------------------------------------------------

export const OPERATIONS_ROUTES = {
  capabilities: '/operations/capabilities',
  list: '/operations',
  detail: '/operations/{operationId}',
  control: '/operations/control',
  killSwitch: '/operations/control/kill-switch',
} as const;

/**
 * E2 operations *action* routes. Cancellation is owned by the E2
 * approval/decision service, not the E4 control plane, so it is kept in a
 * separate map: the proxy forwards these to the E2 action base URL
 * (GBAW_OPERATIONS_ACTION_API_BASE_URL), never the E4 control base.
 */
export const OPERATIONS_ACTION_ROUTES = {
  cancel: '/operations/{operationId}/cancel',
} as const;

export const MAX_PAGE_SIZE = 50;

export const OPERATION_STATES = [
  'prepared',
  'pending_approval',
  'approved',
  'dispatched',
  'executing',
  'retry_pending',
  'succeeded',
  'failed',
  'rejected',
  'cancelled',
  'expired',
] as const;
export type OperationState = (typeof OPERATION_STATES)[number];

/**
 * Pre-dispatch, non-terminal states an operator may cancel. Must match the
 * backend E2 decision service (`_CANCELLABLE_STATES` in operations/decisions.py):
 * once an operation has dispatched or reached a terminal state it can no longer
 * be cancelled. Expiry, by contrast, is system-owned and never an operator
 * action, so `expired` is not — and must never be — cancellable.
 */
export const CANCELLABLE_STATES = ['prepared', 'pending_approval', 'approved'] as const;

/** True when an operation is in a state an operator may cancel. */
export function isCancellable(state: OperationState): boolean {
  return (CANCELLABLE_STATES as readonly string[]).includes(state);
}

export const AUTHORITIES = ['disabled', 'observe', 'advise', 'remediate', 'operate'] as const;
export type Authority = (typeof AUTHORITIES)[number];

export const PHASE_NAMES = [
  'prepare',
  'approve',
  'dispatch',
  'execute',
  'verify',
  'rollback',
] as const;
export type PhaseName = (typeof PHASE_NAMES)[number];

export const PHASE_STATUSES = [
  'not_started',
  'in_progress',
  'succeeded',
  'failed',
  'skipped',
] as const;
export type PhaseStatus = (typeof PHASE_STATUSES)[number];

export const VISIBILITY_OUTCOMES = ['not_applicable', 'pending', 'succeeded', 'failed'] as const;
export type VisibilityOutcome = (typeof VISIBILITY_OUTCOMES)[number];

export const ROLLBACK_OUTCOMES = [...VISIBILITY_OUTCOMES, 'not_recorded'] as const;
export type RollbackOutcome = (typeof ROLLBACK_OUTCOMES)[number];

export const EVIDENCE_CATEGORIES = [
  'authorization',
  'approval',
  'dispatch',
  'verification',
  'rollback',
  'state_change',
] as const;
export type EvidenceCategory = (typeof EVIDENCE_CATEGORIES)[number];

export const CONTROL_OUTCOMES = ['applied', 'version_conflict', 'denied'] as const;
export type ControlOutcome = (typeof CONTROL_OUTCOMES)[number];

export const CONTROL_REASON_CODES = [
  'APPLIED',
  'VERSION_CONFLICT',
  'PHASE_ORDER_INVALID',
  'AUTHORITY_DENIED',
] as const;
export type ControlReasonCode = (typeof CONTROL_REASON_CODES)[number];

export const GATE_KINDS = ['static', 'dynamic'] as const;
export type GateKind = (typeof GATE_KINDS)[number];

export const CONTRACT_VERSION = '1.0';
const OPERATION_ID_PATTERN = /^op_[a-z0-9]{26}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const CURSOR_PATTERN = /^[A-Za-z0-9_-]+$/;
const CURSOR_MAX_LENGTH = 512;
const CAPABILITY_VERSION_PATTERN = /^[1-9][0-9]*\.[0-9]+$/;
const EVIDENCE_SUMMARY_MAX = 280;
const MAX_SAFE = 9007199254740991;

/**
 * Field names that a public-safe projection must never carry. If any of these
 * appears anywhere in a backend payload we fail closed — the UI must never be
 * the thing that leaks identity or a raw provider payload.
 */
const FORBIDDEN_FIELDS = new Set<string>([
  'email',
  'display_name',
  'displayname',
  'token',
  'access_token',
  'id_token',
  'refresh_token',
  'arn',
  'account_id',
  'accountid',
  'account',
  'fleet_id',
  'fleetid',
  'raw_payload',
  'raw_response',
  'provider_payload',
  'provider_response',
  'principal',
  'credential',
  'credentials',
]);

// ---------------------------------------------------------------------------
// Types (the validated, browser-safe projections)
// ---------------------------------------------------------------------------

export interface Gate {
  gate_id: string;
  kind: GateKind;
  satisfied: boolean;
}

export interface CapabilityPhases {
  prepare: boolean;
  dispatch: boolean;
  execute: boolean;
}

export interface CapabilityDiscovery {
  capability_id: string;
  capability_version: string;
  available: boolean;
  provisioned: boolean;
  enabled: boolean;
  effective_authority: Authority;
  phases: CapabilityPhases;
  gates: Gate[];
}

export interface CapabilityDiscoveryDocument {
  contract_version: string;
  generated_at: string;
  deployment_mode: Authority;
  operations_enabled: boolean;
  kill_switch_config_version?: number;
  capabilities: CapabilityDiscovery[];
}

export interface OperationSummary {
  operation_id: string;
  capability_id: string;
  state: OperationState;
  created_at: string;
  updated_at: string;
}

export interface OperationsListResponse {
  contract_version: string;
  page_size: number;
  operations: OperationSummary[];
  next_cursor?: string;
}

export interface PhaseEntry {
  phase: PhaseName;
  status: PhaseStatus;
  occurred_at?: string;
}

export interface Visibility {
  applicable: boolean;
  outcome: VisibilityOutcome;
}

export interface RollbackVisibility {
  applicable: boolean;
  outcome: RollbackOutcome;
}

export interface EvidenceEntry {
  category: EvidenceCategory;
  summary: string;
  recorded_at?: string;
}

export interface OperationDetail {
  contract_version: string;
  operation_id: string;
  capability_id: string;
  state: OperationState;
  created_at: string;
  updated_at: string;
  phases: PhaseEntry[];
  verification: Visibility;
  rollback: RollbackVisibility;
  evidence: EvidenceEntry[];
}

export interface KillSwitchCapability {
  prepare: boolean;
  dispatch: boolean;
  execute: boolean;
}

export interface KillSwitchDocument {
  contract_version: string;
  config_version: number;
  issued_at: string;
  not_after: string;
  operations_enabled: boolean;
  capabilities: {
    'gamelift.capacity-adjustment': KillSwitchCapability;
  };
}

export interface ControlResponse {
  contract_version: string;
  outcome: ControlOutcome;
  config_version: number;
  reason_code?: ControlReasonCode;
  effective?: KillSwitchDocument;
}

export interface DesiredState {
  operations_enabled: boolean;
  capabilities: {
    'gamelift.capacity-adjustment': KillSwitchCapability;
  };
}

export interface ControlRequest {
  contract_version: string;
  expected_config_version: number;
  desired: DesiredState;
}

export interface ListRequest {
  contract_version: string;
  page_size?: number;
  cursor?: string;
  filter?: {
    capability_id?: string;
    states?: OperationState[];
  };
}

// ---------------------------------------------------------------------------
// Validation primitives
// ---------------------------------------------------------------------------

export class SchemaGuardError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SchemaGuardError';
  }
}

function fail(path: string, why: string): never {
  throw new SchemaGuardError(`${path}: ${why}`);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Recursively assert that no forbidden field name appears anywhere in the
 * payload. Runs before shape checks so a leak fails closed even if the rest of
 * the shape happens to be valid.
 */
function assertNoForbiddenFields(value: unknown, path: string): void {
  if (Array.isArray(value)) {
    value.forEach((item, i) => assertNoForbiddenFields(item, `${path}[${i}]`));
    return;
  }
  if (isPlainObject(value)) {
    for (const key of Object.keys(value)) {
      if (FORBIDDEN_FIELDS.has(key.toLowerCase())) {
        fail(`${path}.${key}`, 'forbidden field present in a public-safe projection');
      }
      assertNoForbiddenFields(value[key], `${path}.${key}`);
    }
  }
}

function obj(value: unknown, path: string): Record<string, unknown> {
  if (!isPlainObject(value)) fail(path, 'expected an object');
  return value;
}

function requireExactKeys(
  value: Record<string, unknown>,
  path: string,
  required: string[],
  optional: string[] = [],
): void {
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${path}.${key}`, 'unknown field (closed object)');
  }
  for (const key of required) {
    if (!(key in value)) fail(`${path}.${key}`, 'required field missing');
  }
}

function str(value: unknown, path: string): string {
  if (typeof value !== 'string') fail(path, 'expected a string');
  return value;
}

function bool(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') fail(path, 'expected a boolean');
  return value;
}

function int(value: unknown, path: string, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) fail(path, 'expected an integer');
  if (value < min || value > max) fail(path, `out of range [${min}, ${max}]`);
  return value;
}

function enumValue<T extends string>(value: unknown, path: string, allowed: readonly T[]): T {
  const s = str(value, path);
  if (!(allowed as readonly string[]).includes(s)) {
    fail(path, `not one of ${allowed.join(', ')}`);
  }
  return s as T;
}

function pattern(value: unknown, path: string, re: RegExp): string {
  const s = str(value, path);
  if (!re.test(s)) fail(path, `does not match ${re}`);
  return s;
}

function timestamp(value: unknown, path: string): string {
  const s = str(value, path);
  // date-time (RFC 3339). We accept any parseable ISO instant; the backend
  // already constrains the wire form, so the UI only needs a sane, renderable value.
  if (Number.isNaN(Date.parse(s))) fail(path, 'not a valid date-time');
  return s;
}

function contractVersion(value: unknown, path: string): string {
  const s = str(value, path);
  if (s !== CONTRACT_VERSION) fail(path, `expected contract_version "${CONTRACT_VERSION}"`);
  return s;
}

function array(value: unknown, path: string, min: number, max: number): unknown[] {
  if (!Array.isArray(value)) fail(path, 'expected an array');
  if (value.length < min || value.length > max) {
    fail(path, `array length out of range [${min}, ${max}]`);
  }
  return value;
}

// ---------------------------------------------------------------------------
// Parsers
// ---------------------------------------------------------------------------

function parseGate(value: unknown, path: string): Gate {
  const o = obj(value, path);
  requireExactKeys(o, path, ['gate_id', 'kind', 'satisfied']);
  return {
    gate_id: pattern(o.gate_id, `${path}.gate_id`, IDENTIFIER_PATTERN),
    kind: enumValue(o.kind, `${path}.kind`, GATE_KINDS),
    satisfied: bool(o.satisfied, `${path}.satisfied`),
  };
}

function parseCapabilityPhases(value: unknown, path: string): CapabilityPhases {
  const o = obj(value, path);
  requireExactKeys(o, path, ['prepare', 'dispatch', 'execute']);
  return {
    prepare: bool(o.prepare, `${path}.prepare`),
    dispatch: bool(o.dispatch, `${path}.dispatch`),
    execute: bool(o.execute, `${path}.execute`),
  };
}

function parseCapability(value: unknown, path: string): CapabilityDiscovery {
  const o = obj(value, path);
  requireExactKeys(o, path, [
    'capability_id',
    'capability_version',
    'available',
    'provisioned',
    'enabled',
    'effective_authority',
    'phases',
    'gates',
  ]);
  const gates = array(o.gates, `${path}.gates`, 1, 32).map((g, i) =>
    parseGate(g, `${path}.gates[${i}]`),
  );
  return {
    capability_id: pattern(o.capability_id, `${path}.capability_id`, IDENTIFIER_PATTERN),
    capability_version: pattern(
      o.capability_version,
      `${path}.capability_version`,
      CAPABILITY_VERSION_PATTERN,
    ),
    available: bool(o.available, `${path}.available`),
    provisioned: bool(o.provisioned, `${path}.provisioned`),
    enabled: bool(o.enabled, `${path}.enabled`),
    effective_authority: enumValue(
      o.effective_authority,
      `${path}.effective_authority`,
      AUTHORITIES,
    ),
    phases: parseCapabilityPhases(o.phases, `${path}.phases`),
    gates,
  };
}

export function parseCapabilityDiscovery(value: unknown): CapabilityDiscoveryDocument {
  const path = 'capability-discovery';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(
    o,
    path,
    ['contract_version', 'generated_at', 'deployment_mode', 'operations_enabled', 'capabilities'],
    ['kill_switch_config_version'],
  );
  const capabilities = array(o.capabilities, `${path}.capabilities`, 0, 32).map((c, i) =>
    parseCapability(c, `${path}.capabilities[${i}]`),
  );
  const doc: CapabilityDiscoveryDocument = {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    generated_at: timestamp(o.generated_at, `${path}.generated_at`),
    deployment_mode: enumValue(o.deployment_mode, `${path}.deployment_mode`, AUTHORITIES),
    operations_enabled: bool(o.operations_enabled, `${path}.operations_enabled`),
    capabilities,
  };
  if ('kill_switch_config_version' in o) {
    doc.kill_switch_config_version = int(
      o.kill_switch_config_version,
      `${path}.kill_switch_config_version`,
      1,
      MAX_SAFE,
    );
  }
  return doc;
}

function parseOperationSummary(value: unknown, path: string): OperationSummary {
  const o = obj(value, path);
  requireExactKeys(o, path, [
    'operation_id',
    'capability_id',
    'state',
    'created_at',
    'updated_at',
  ]);
  return {
    operation_id: pattern(o.operation_id, `${path}.operation_id`, OPERATION_ID_PATTERN),
    capability_id: pattern(o.capability_id, `${path}.capability_id`, IDENTIFIER_PATTERN),
    state: enumValue(o.state, `${path}.state`, OPERATION_STATES),
    created_at: timestamp(o.created_at, `${path}.created_at`),
    updated_at: timestamp(o.updated_at, `${path}.updated_at`),
  };
}

export function parseOperationsListResponse(value: unknown): OperationsListResponse {
  const path = 'operations-list';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(o, path, ['contract_version', 'page_size', 'operations'], ['next_cursor']);
  const operations = array(o.operations, `${path}.operations`, 0, MAX_PAGE_SIZE).map((op, i) =>
    parseOperationSummary(op, `${path}.operations[${i}]`),
  );
  const pageSize = int(o.page_size, `${path}.page_size`, 1, MAX_PAGE_SIZE);
  if (operations.length > pageSize) {
    fail(`${path}.operations`, 'more operations than the declared page_size');
  }
  const result: OperationsListResponse = {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    page_size: pageSize,
    operations,
  };
  if ('next_cursor' in o) {
    const cursor = pattern(o.next_cursor, `${path}.next_cursor`, CURSOR_PATTERN);
    if (cursor.length < 1 || cursor.length > CURSOR_MAX_LENGTH) {
      fail(`${path}.next_cursor`, 'cursor length out of bounds');
    }
    result.next_cursor = cursor;
  }
  return result;
}

function parsePhaseEntry(value: unknown, path: string): PhaseEntry {
  const o = obj(value, path);
  requireExactKeys(o, path, ['phase', 'status'], ['occurred_at']);
  const entry: PhaseEntry = {
    phase: enumValue(o.phase, `${path}.phase`, PHASE_NAMES),
    status: enumValue(o.status, `${path}.status`, PHASE_STATUSES),
  };
  if ('occurred_at' in o) {
    entry.occurred_at = timestamp(o.occurred_at, `${path}.occurred_at`);
  }
  return entry;
}

function parseVisibility(value: unknown, path: string): Visibility {
  const o = obj(value, path);
  requireExactKeys(o, path, ['applicable', 'outcome']);
  return {
    applicable: bool(o.applicable, `${path}.applicable`),
    outcome: enumValue(o.outcome, `${path}.outcome`, VISIBILITY_OUTCOMES),
  };
}

function parseRollbackVisibility(value: unknown, path: string): RollbackVisibility {
  const o = obj(value, path);
  requireExactKeys(o, path, ['applicable', 'outcome']);
  return {
    applicable: bool(o.applicable, `${path}.applicable`),
    outcome: enumValue(o.outcome, `${path}.outcome`, ROLLBACK_OUTCOMES),
  };
}

function parseEvidence(value: unknown, path: string): EvidenceEntry {
  const o = obj(value, path);
  requireExactKeys(o, path, ['category', 'summary'], ['recorded_at']);
  const summary = str(o.summary, `${path}.summary`);
  if (summary.length < 1 || summary.length > EVIDENCE_SUMMARY_MAX) {
    fail(`${path}.summary`, `length out of range [1, ${EVIDENCE_SUMMARY_MAX}]`);
  }
  const entry: EvidenceEntry = {
    category: enumValue(o.category, `${path}.category`, EVIDENCE_CATEGORIES),
    summary,
  };
  if ('recorded_at' in o) {
    entry.recorded_at = timestamp(o.recorded_at, `${path}.recorded_at`);
  }
  return entry;
}

export function parseOperationDetail(value: unknown): OperationDetail {
  const path = 'operation-detail';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(o, path, [
    'contract_version',
    'operation_id',
    'capability_id',
    'state',
    'created_at',
    'updated_at',
    'phases',
    'verification',
    'rollback',
    'evidence',
  ]);
  return {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    operation_id: pattern(o.operation_id, `${path}.operation_id`, OPERATION_ID_PATTERN),
    capability_id: pattern(o.capability_id, `${path}.capability_id`, IDENTIFIER_PATTERN),
    state: enumValue(o.state, `${path}.state`, OPERATION_STATES),
    created_at: timestamp(o.created_at, `${path}.created_at`),
    updated_at: timestamp(o.updated_at, `${path}.updated_at`),
    phases: array(o.phases, `${path}.phases`, 1, 8).map((p, i) =>
      parsePhaseEntry(p, `${path}.phases[${i}]`),
    ),
    verification: parseVisibility(o.verification, `${path}.verification`),
    rollback: parseRollbackVisibility(o.rollback, `${path}.rollback`),
    evidence: array(o.evidence, `${path}.evidence`, 0, 16).map((e, i) =>
      parseEvidence(e, `${path}.evidence[${i}]`),
    ),
  };
}

function parseKillSwitchCapability(value: unknown, path: string): KillSwitchCapability {
  const o = obj(value, path);
  requireExactKeys(o, path, ['prepare', 'dispatch', 'execute']);
  return {
    prepare: bool(o.prepare, `${path}.prepare`),
    dispatch: bool(o.dispatch, `${path}.dispatch`),
    execute: bool(o.execute, `${path}.execute`),
  };
}

export function parseKillSwitch(value: unknown): KillSwitchDocument {
  const path = 'kill-switch';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(o, path, [
    'contract_version',
    'config_version',
    'issued_at',
    'not_after',
    'operations_enabled',
    'capabilities',
  ]);
  const caps = obj(o.capabilities, `${path}.capabilities`);
  requireExactKeys(caps, `${path}.capabilities`, ['gamelift.capacity-adjustment']);
  return {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    config_version: int(o.config_version, `${path}.config_version`, 1, MAX_SAFE),
    issued_at: timestamp(o.issued_at, `${path}.issued_at`),
    not_after: timestamp(o.not_after, `${path}.not_after`),
    operations_enabled: bool(o.operations_enabled, `${path}.operations_enabled`),
    capabilities: {
      'gamelift.capacity-adjustment': parseKillSwitchCapability(
        caps['gamelift.capacity-adjustment'],
        `${path}.capabilities["gamelift.capacity-adjustment"]`,
      ),
    },
  };
}

export function parseControlResponse(value: unknown): ControlResponse {
  const path = 'control-response';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(o, path, ['contract_version', 'outcome', 'config_version'], [
    'reason_code',
    'effective',
  ]);
  const result: ControlResponse = {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    outcome: enumValue(o.outcome, `${path}.outcome`, CONTROL_OUTCOMES),
    config_version: int(o.config_version, `${path}.config_version`, 1, MAX_SAFE),
  };
  if ('reason_code' in o) {
    result.reason_code = enumValue(o.reason_code, `${path}.reason_code`, CONTROL_REASON_CODES);
  }
  if ('effective' in o && o.effective !== undefined) {
    result.effective = parseKillSwitch(o.effective);
  }
  return result;
}

export interface CancelResponse {
  operation_id: string;
  new_state: OperationState;
}

/**
 * Validate the upstream cancel response and project it to a minimal, public-safe
 * confirmation. The E2 action API returns a full internal state-change ledger
 * record (prepared-operation hash, actor, correlation ids); the operator UI must
 * not depend on or expose that internal detail, so we assert the state actually
 * transitioned to `cancelled` and surface only the operation id and new state.
 * The detail timeline is re-fetched after a successful cancel for the full view.
 */
export function parseCancelResponse(value: unknown): CancelResponse {
  const path = 'cancel-response';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  const newState = enumValue(o.new_state, `${path}.new_state`, OPERATION_STATES);
  if (newState !== 'cancelled') {
    fail(`${path}.new_state`, 'cancel did not transition the operation to cancelled');
  }
  return {
    operation_id: pattern(o.operation_id, `${path}.operation_id`, OPERATION_ID_PATTERN),
    new_state: newState,
  };
}

// ---------------------------------------------------------------------------
// Request builders (client → proxy). These construct only the frozen,
// identity-free request bodies; the proxy re-validates before forwarding.
// ---------------------------------------------------------------------------

export function buildControlRequest(
  expectedConfigVersion: number,
  desired: DesiredState,
): ControlRequest {
  return {
    contract_version: CONTRACT_VERSION,
    expected_config_version: expectedConfigVersion,
    desired,
  };
}

/**
 * Validate an inbound control request body (used by the proxy to reject any
 * request that carries identity/credential/policy or an out-of-shape body
 * before it reaches the backend).
 */
export function parseControlRequest(value: unknown): ControlRequest {
  const path = 'control-request';
  assertNoForbiddenFields(value, path);
  const o = obj(value, path);
  requireExactKeys(o, path, ['contract_version', 'expected_config_version', 'desired']);
  const desired = obj(o.desired, `${path}.desired`);
  requireExactKeys(desired, `${path}.desired`, ['operations_enabled', 'capabilities']);
  const caps = obj(desired.capabilities, `${path}.desired.capabilities`);
  requireExactKeys(caps, `${path}.desired.capabilities`, ['gamelift.capacity-adjustment']);
  return {
    contract_version: contractVersion(o.contract_version, `${path}.contract_version`),
    expected_config_version: int(
      o.expected_config_version,
      `${path}.expected_config_version`,
      1,
      MAX_SAFE,
    ),
    desired: {
      operations_enabled: bool(desired.operations_enabled, `${path}.desired.operations_enabled`),
      capabilities: {
        'gamelift.capacity-adjustment': parseKillSwitchCapability(
          caps['gamelift.capacity-adjustment'],
          `${path}.desired.capabilities["gamelift.capacity-adjustment"]`,
        ),
      },
    },
  };
}
