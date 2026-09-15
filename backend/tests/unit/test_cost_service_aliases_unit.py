"""Unit tests for deterministic service alias and family resolution.

These tests use real Cost Explorer canonical ``SERVICE`` dimension values (for
example ``EC2 - Other`` and ``Amazon Simple Storage Service``) paired with
synthetic monetary amounts. They exercise the pure resolver directly and, where
rounding, share, and "no new query" guarantees matter, drive it through
``CostReportService.reuse_report`` against a cached snapshot.
"""

# Standard library
from datetime import datetime, timezone
from unittest.mock import MagicMock

# Third-party packages
import pytest

# Local modules
from agents.cost_report import CostReportCache, CostReportError, CostReportService
from agents.cost_service_aliases import ServiceResolution, resolve_service_selection

pytestmark = pytest.mark.unit

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

# Real Cost Explorer canonical SERVICE dimension values.
_EC2_OTHER = "EC2 - Other"
_EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
_S3 = "Amazon Simple Storage Service"
_RDS = "Amazon Relational Database Service"
_LAMBDA = "AWS Lambda"


def _group(service: str, amount: str, unit: str = "USD", metric: str = "UnblendedCost") -> dict:
    return {
        "Keys": [service],
        "Metrics": {metric: {"Amount": amount, "Unit": unit}},
    }


def _page(groups: list[dict], *, estimated: bool = False) -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-05-01", "End": "2026-05-16"},
                "Estimated": estimated,
                "Groups": groups,
            }
        ]
    }


def _service(client: MagicMock, report_id: str = "cost-alias-fixture") -> CostReportService:
    return CostReportService(
        client_factory=lambda: client,
        cache=CostReportCache(maxsize=8, ttl_seconds=300),
        now=lambda: _FIXED_NOW,
        report_id_factory=lambda: report_id,
    )


class TestResolverPureFunction:
    """Direct tests of the pure alias/family resolver."""

    def test_exact_match_takes_precedence_and_is_case_sensitive_first(self):
        available = [_EC2_OTHER, _EC2_COMPUTE, _S3]

        resolution = resolve_service_selection([_S3], available)

        assert resolution == ServiceResolution(resolved=(_S3,), missing=())

    def test_case_insensitive_exact_canonical_match(self):
        available = [_S3, _LAMBDA]

        resolution = resolve_service_selection(["amazon simple storage service"], available)

        # Emits the canonical casing from the snapshot, not the requested casing.
        assert resolution.resolved == (_S3,)
        assert resolution.missing == ()

    def test_inputs_are_trimmed_before_matching(self):
        available = [_S3, _EC2_OTHER, _EC2_COMPUTE]

        resolution = resolve_service_selection(["  Amazon S3  ", "\tEC2 "], available)

        assert resolution.resolved == (_S3, _EC2_OTHER, _EC2_COMPUTE)
        assert resolution.missing == ()

    @pytest.mark.parametrize("alias", ["EC2", "Amazon EC2", "ec2", "amazon ec2", "eC2"])
    def test_ec2_alias_expands_to_full_family_in_fixed_order(self, alias: str):
        # Snapshot deliberately lists the members in the opposite order to prove
        # the resolver imposes its own fixed family order.
        available = [_EC2_COMPUTE, _EC2_OTHER, _S3]

        resolution = resolve_service_selection([alias], available)

        assert resolution.resolved == (_EC2_OTHER, _EC2_COMPUTE)
        assert resolution.missing == ()

    @pytest.mark.parametrize("alias", ["S3", "Amazon S3", "s3", "amazon s3"])
    def test_s3_alias_maps_to_canonical_service(self, alias: str):
        available = [_S3, _EC2_OTHER]

        resolution = resolve_service_selection([alias], available)

        assert resolution.resolved == (_S3,)
        assert resolution.missing == ()

    def test_family_expansion_includes_only_present_members(self):
        # Only one EC2 member present in the snapshot.
        available = [_EC2_COMPUTE, _S3]

        resolution = resolve_service_selection(["EC2"], available)

        assert resolution.resolved == (_EC2_COMPUTE,)
        assert resolution.missing == ()

    def test_alias_with_no_present_members_is_missing(self):
        available = [_S3, _LAMBDA]

        resolution = resolve_service_selection(["EC2"], available)

        assert resolution.resolved == ()
        assert resolution.missing == ("EC2",)

    def test_unknown_alias_is_missing_and_preserves_original_string(self):
        available = [_EC2_OTHER, _EC2_COMPUTE, _S3]

        resolution = resolve_service_selection(["  Amazon Nonexistent  "], available)

        assert resolution.resolved == ()
        assert resolution.missing == ("  Amazon Nonexistent  ",)

    def test_mixed_alias_and_exact_requests_preserve_order(self):
        available = [_EC2_OTHER, _EC2_COMPUTE, _S3, _RDS]

        resolution = resolve_service_selection([_RDS, "EC2", "Amazon S3"], available)

        assert resolution.resolved == (_RDS, _EC2_OTHER, _EC2_COMPUTE, _S3)
        assert resolution.missing == ()

    def test_deduplication_across_alias_and_exact_targets(self):
        available = [_EC2_OTHER, _EC2_COMPUTE, _S3]

        # EC2 alias expands to both members; the explicit exact request for one
        # member and a duplicate alias must not repeat any canonical target.
        resolution = resolve_service_selection(
            ["EC2", _EC2_COMPUTE, "Amazon EC2", "Amazon S3", "s3"],
            available,
        )

        assert resolution.resolved == (_EC2_OTHER, _EC2_COMPUTE, _S3)
        assert resolution.missing == ()

    def test_partial_missing_is_reported_alongside_resolved(self):
        available = [_S3, _EC2_OTHER, _EC2_COMPUTE]

        resolution = resolve_service_selection(["Amazon S3", "Totally Unknown"], available)

        assert resolution.resolved == (_S3,)
        assert resolution.missing == ("Totally Unknown",)

    def test_exact_canonical_alias_key_beats_alias_family_expansion(self):
        # Synthetic collision: the snapshot contains a canonical service named
        # literally "EC2" (an exact match for the EC2 alias key) alongside the
        # real family members. An exact request for "EC2" must resolve to that
        # canonical entry only and must never expand to the family.
        synthetic_ec2 = "EC2"
        available = [synthetic_ec2, _EC2_OTHER, _EC2_COMPUTE, _S3]

        exact = resolve_service_selection(["EC2"], available)
        assert exact.resolved == (synthetic_ec2,)
        assert exact.missing == ()

        # A case-insensitive request also matches the canonical entry (exact
        # over alias) rather than expanding the family, because "EC2" is the
        # only canonical name that casefolds to "ec2" in this snapshot.
        case_insensitive = resolve_service_selection(["ec2"], available)
        assert case_insensitive.resolved == (synthetic_ec2,)
        assert case_insensitive.missing == ()

    def test_casefold_collision_fails_closed_but_preserves_exact_case_sensitive(self):
        # Two distinct canonical names collide under casefold. Neither is an
        # alias key, so this isolates the collision behavior.
        lambda_lower = "aws lambda"
        available = [_LAMBDA, lambda_lower]
        assert _LAMBDA.casefold() == lambda_lower.casefold()

        # Case-sensitive exact matches still resolve to the requested casing.
        assert resolve_service_selection([_LAMBDA], available) == ServiceResolution(resolved=(_LAMBDA,), missing=())
        assert resolve_service_selection([lambda_lower], available) == ServiceResolution(
            resolved=(lambda_lower,), missing=()
        )

        # A case-insensitive-only request is ambiguous and fails closed rather
        # than choosing a winner by snapshot iteration order.
        ambiguous = resolve_service_selection(["AWS LAMBDA"], available)
        assert ambiguous.resolved == ()
        assert ambiguous.missing == ("AWS LAMBDA",)


class TestAliasResolutionThroughReuse:
    """Alias resolution exercised through the cached-snapshot reuse path."""

    def _snapshot_service(self) -> tuple[CostReportService, str, MagicMock]:
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group(_EC2_COMPUTE, "40.00"),
                _group(_EC2_OTHER, "10.00"),
                _group(_S3, "25.00"),
                _group(_RDS, "25.00"),
            ]
        )
        service = _service(client, report_id="cost-alias-reuse")
        snapshot = service.create_report("2026-05-01", "2026-05-15")
        # One logical query so far; alias resolution must not add more.
        client.get_cost_and_usage.assert_called_once()
        return service, snapshot.report.report_id, client

    def test_ec2_and_s3_aliases_resolve_round_and_share_without_new_query(self):
        service, report_id, client = self._snapshot_service()

        selection = service.reuse_report(report_id, ["Amazon EC2", "S3"])

        # 40 + 10 (EC2 family) + 25 (S3) = 75 of a 100.00 total => 75.0%.
        assert selection.services == [_EC2_OTHER, _EC2_COMPUTE, _S3]
        assert selection.raw_amount == "75.00"
        assert selection.amount == "75.00"
        assert selection.percentage == "75.0"
        assert selection.snapshot_reused is True
        # No additional Cost Explorer call was made during the follow-up.
        client.get_cost_and_usage.assert_called_once()

    def test_mixed_exact_and_alias_dedup_through_reuse(self):
        service, report_id, client = self._snapshot_service()

        selection = service.reuse_report(
            report_id,
            [_EC2_COMPUTE, "EC2", "amazon s3", "Amazon S3"],
        )

        assert selection.services == [_EC2_COMPUTE, _EC2_OTHER, _S3]
        # 40 + 10 + 25 = 75.00 of 100.00 => 75.0%.
        assert selection.amount == "75.00"
        assert selection.percentage == "75.0"
        client.get_cost_and_usage.assert_called_once()

    def test_unknown_alias_fails_closed_without_new_query(self):
        service, report_id, client = self._snapshot_service()

        with pytest.raises(CostReportError) as raised:
            service.reuse_report(report_id, ["Amazon EC2", "Amazon Nonexistent"])

        assert raised.value.code == "SERVICE_NOT_IN_REPORT"
        assert raised.value.retryable is False
        client.get_cost_and_usage.assert_called_once()

    def test_alias_missing_all_family_members_fails_closed(self):
        client = MagicMock()
        client.get_cost_and_usage.return_value = _page(
            [
                _group(_S3, "25.00"),
                _group(_RDS, "75.00"),
            ]
        )
        service = _service(client, report_id="cost-alias-no-ec2")
        report_id = service.create_report("2026-05-01", "2026-05-15").report.report_id

        with pytest.raises(CostReportError) as raised:
            service.reuse_report(report_id, ["EC2"])

        assert raised.value.code == "SERVICE_NOT_IN_REPORT"
        client.get_cost_and_usage.assert_called_once()
