"""Robustness tests for ``list_gamelift_fleets`` (#464).

These tests pin the fleet-listing projection's behavior under pagination,
malformed provider shapes, and hostile field values:

* residual ``list_fleets`` pagination is decided from the classic fleet IDs that
  remain after container-ID exclusion, from any resume token the capped
  paginator left behind, or from the raw read reaching its ceiling (which, under
  botocore's paginator, subsumes the resume-token signal), so an account with
  more classic fleets than the cap, or one whose repeated/interleaved IDs fill
  the read before every classic ID is seen, is never reported as complete;
* malformed container shapes neither escape the tool nor discard the classic
  fleet rows already fetched;
* a mandatory ``FleetId`` keeps canonical 8-4-4-4-12 UUID-tailed fleet IDs even
  when the final group is all decimal digits, while rejecting other hostile
  12-digit values;
* a total non-denied listing failure carries a typed error code;
* a terminal malformed listing carries no ``truncated`` marker;
* the warning distinct-entry cap and overflow ``Count`` reflect total folded
  occurrences;
* every provider operation the tool touches stays within the allowed read set,
  including calls placed inside a ``try`` block;
* list-time fallbacks set ``stale``;
* AWS enum fields are validated against allowlists so hostname-shaped values are
  dropped;
* a wrong-typed describe response is not amplified into per-ID fallbacks;
* deployment reads are bounded to the latest deployment only.

Every case asserts the behavioral outcome (status, flags, warning codes, and
retained rows) rather than merely that the tool returned, and uses synthetic
hostile values only.
"""

# Standard library
import json
from typing import Any
from unittest.mock import MagicMock, patch

# Third-party packages
import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

pytestmark = pytest.mark.unit

SYNTHETIC_ACCOUNT_ID = "123456789012"
SYNTHETIC_FLEET_ARN = f"arn:aws:gamelift:us-west-2:{SYNTHETIC_ACCOUNT_ID}:fleet/fleet-classic-0001"
SYNTHETIC_URL = "https://internal.example.invalid/secret?token=abc123"
SYNTHETIC_IP = "10.11.12.13"


def _assert_no_sensitive(payload: dict[str, Any]) -> None:
    blob = json.dumps(payload, default=str)
    assert SYNTHETIC_ACCOUNT_ID not in blob, f"account id leaked: {blob}"
    assert "arn:aws" not in blob, f"ARN leaked: {blob}"
    assert "example.invalid" not in blob, f"coordinate leaked: {blob}"
    assert SYNTHETIC_IP not in blob, f"IP leaked: {blob}"


def _digit_tail_fleet_id() -> str:
    # Canonical fleet-<8-4-4-4-12 hex uuid> whose 12-char last group is all
    # decimal digits (built from fragments to avoid a literal 12-digit run).
    tail = "".join(str((i * 7) % 10) for i in range(12))
    return "fleet-2222bbbb-33cc-44dd-55ee-" + tail


# ---------------------------------------------------------------------------
# Recording proxy fake: records EVERY accessed operation via __getattr__.
# ---------------------------------------------------------------------------
class RecordingGameLift:
    """A GameLift client fake whose every attribute access is recorded.

    Unlike a fake that only implements the allowed operations, this records any
    operation the tool touches — including one placed inside a ``try`` — so an
    unexpected provider call cannot hide behind an exception handler.
    """

    def __init__(self, **cfg: Any) -> None:
        self._cfg = cfg
        self.invoked_operations: list[str] = []

    def __getattr__(self, name: str) -> Any:
        # Record the access and return a callable (direct op) or paginator source.
        object.__getattribute__(self, "invoked_operations").append(name)
        cfg = object.__getattribute__(self, "_cfg")

        if name == "get_paginator":

            def get_paginator(operation_name: str) -> MagicMock:
                object.__getattribute__(self, "invoked_operations").append(operation_name)
                pages_or_exc = cfg.get(operation_name, [])
                pag = MagicMock()
                if isinstance(pages_or_exc, Exception):
                    pag.paginate.side_effect = pages_or_exc
                else:
                    pag.paginate.return_value = pages_or_exc
                return pag

            return get_paginator

        def direct(**kwargs: Any) -> Any:
            handler = cfg.get(name)
            if isinstance(handler, Exception):
                raise handler
            if callable(handler):
                return handler(**kwargs)
            return handler

        return direct


def _run(fake: Any) -> dict[str, Any]:
    # Local modules
    from agents.gamelift_specialist import list_gamelift_fleets

    with patch("agents.gamelift_specialist.boto3.client", return_value=fake):
        return list_gamelift_fleets()


# ---------------------------------------------------------------------------
# Canonical UUID-tailed fleet IDs kept; hostile 12-digit values rejected.
# ---------------------------------------------------------------------------
class TestCanonicalFleetIdGrammar:
    def test_digit_tailed_uuid_fleet_id_is_kept(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        fid = _digit_tail_fleet_id()
        row = project_classic_fleet({"FleetId": fid, "Status": "ACTIVE"})
        assert row.get("FleetId") == fid, "canonical digit-tailed UUID fleet id was wrongly discarded"

    def test_container_fleet_canonical_id_is_kept(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        tail = "".join(str((i * 3 + 1) % 10) for i in range(12))
        fid = "containerfleet-aaaa1111-2222-4333-8444-" + tail
        row = project_classic_fleet({"FleetId": fid, "Status": "ACTIVE"})
        assert row.get("FleetId") == fid

    def test_short_non_uuid_fleet_ids_still_accepted(self):
        # Existing fixtures (fleet-int-0001, fleet-aaaa-0001) must keep working.
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        for fid in ("fleet-int-0001", "fleet-aaaa-0001", "fleet-classic-0001"):
            row = project_classic_fleet({"FleetId": fid, "Status": "ACTIVE"})
            assert row.get("FleetId") == fid, f"{fid} should remain valid"

    def test_arn_shaped_fleet_id_still_rejected(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        assert project_classic_fleet({"FleetId": SYNTHETIC_FLEET_ARN, "Status": "ACTIVE"}) == {}

    def test_bare_12_digit_and_padded_values_rejected(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        # Build hostile 12-digit values from fragments; none is the canonical
        # fleet-<uuid> shape, so the digit-run rule must still reject them.
        bare = "0000" + "1111" + "2222"
        padded = "fleet-prod-" + bare  # digit run, not canonical UUID
        upper = "FLEET-2222BBBB-33CC-44DD-55EE-" + "".join(str((i * 7) % 10) for i in range(12))
        for fid in (bare, padded, upper):
            assert project_classic_fleet({"FleetId": fid, "Status": "ACTIVE"}) == {}, f"{fid} should be rejected"

    def test_detail_tool_short_legacy_fleet_ids_stay_valid(self):
        # The utilization/capacity/scaling FleetId fields keep accepting the
        # short non-UUID ids the detail tools use.
        # Local modules
        from agents.gamelift_projections import project_fleet_utilization

        result = project_fleet_utilization(
            {
                "FleetUtilization": [
                    {"FleetId": "fleet-aaaa-0001", "ActiveServerProcessCount": 3, "Location": "us-west-2"}
                ]
            }
        )
        assert result["status"] == "ok"
        assert result["FleetUtilization"][0]["FleetId"] == "fleet-aaaa-0001"


# ---------------------------------------------------------------------------
# Residual list_fleets pagination decided on classic IDs after container exclusion.
# ---------------------------------------------------------------------------
def _stub_client():
    # The unit autouse fixture patches ``boto3.client`` globally, so build the
    # real GameLift client through a fresh Session (whose ``client`` is not
    # patched) to drive the real botocore paginator against a Stubber.
    session = boto3.Session()
    client = session.client(
        "gamelift",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(retries={"max_attempts": 0}),
    )
    return client, Stubber(client)


class TestResidualPaginationAfterExclusion:
    def test_150_classic_plus_1_container_is_not_ok(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        container_id = "containerfleet-0000aaaa-1111-4bbb-8ccc-dddddddd0001"
        classic_ids = [f"fleet-{i:08x}-aaaa-4bbb-8ccc-{i:04d}dddddddd" for i in range(150)]

        stub.add_response(
            "list_container_fleets",
            {"ContainerFleets": [{"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}]},
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        stub.add_response(
            "describe_container_fleet",
            {"ContainerFleet": {"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
        )
        stub.add_response("list_fleet_deployments", {"FleetDeployments": []})
        # ListFleets returns the container ID mixed into the first page.
        stub.add_response("list_fleets", {"FleetIds": [container_id] + classic_ids[:99], "NextToken": "page-2"})
        stub.add_response("list_fleets", {"FleetIds": classic_ids[99:]})
        described = classic_ids[:100]
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in described]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        # 150 classic fleets exist but only 100 are returned: the view MUST NOT
        # read as a complete ok inventory.
        assert result["status"] != "ok", "residual classic pagination read as ok"
        assert result.get("truncated") is True
        assert result.get("paginated") is True

    def test_paginate_passes_pagination_config_maxitems(self):
        # The classic list path must forward PaginationConfig; dropping it must
        # fail this test. Observe the actual paginate kwargs.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        captured: dict[str, Any] = {}

        class _Fake:
            def __init__(self) -> None:
                self.invoked_operations: list[str] = []

            def get_paginator(self, operation_name: str) -> MagicMock:
                pag = MagicMock()
                if operation_name == "list_fleets":

                    def paginate(**kwargs: Any):
                        captured["list_fleets"] = kwargs
                        return [{"FleetIds": ["fleet-aaaa-0001"]}]

                    pag.paginate.side_effect = paginate
                else:
                    pag.paginate.return_value = (
                        [{"ContainerFleets": []}] if operation_name == "list_container_fleets" else [{}]
                    )
                return pag

            def describe_fleet_attributes(self, **kw: Any):
                return {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in kw["FleetIds"]]}

        with patch("agents.gamelift_specialist.boto3.client", return_value=_Fake()):
            list_gamelift_fleets()

        assert "list_fleets" in captured, "list_fleets paginate was not called"
        pconfig = captured["list_fleets"].get("PaginationConfig")
        assert pconfig and "MaxItems" in pconfig, "classic list_fleets must bound pages with PaginationConfig MaxItems"


# ---------------------------------------------------------------------------
# Malformed container shapes must not escape the tool or drop classic rows.
#
# ``_GOOD_CONTAINER`` is module-scoped so the parametrized lambdas can close over
# it: a lambda defined in a class body cannot see the class's attributes when it
# runs, so a class-scoped definition would make every lambda raise ``NameError``
# and silently take the describe-failure fallback instead of exercising its named
# shape.
# ---------------------------------------------------------------------------
_GOOD_CONTAINER = {"FleetId": "cf-good", "Status": "ACTIVE", "InstanceType": "c6i.large"}
_CLASSIC_OK = {"FleetAttributes": [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]}


def _container_base_cfg() -> dict[str, Any]:
    return {
        "list_fleets": [{"FleetIds": ["fleet-classic-0001"]}],
        "describe_fleet_attributes": _CLASSIC_OK,
        "list_container_group_definitions": [{"ContainerGroupDefinitions": []}],
        "list_fleet_deployments": [{"FleetDeployments": []}],
    }


class TestContainerShapeSafety:
    # Each case: (label, overrides, container_kept, expect_malformed, expect_stale, expect_code).
    # ``container_kept`` — whether a projected container row survives.
    # ``expect_malformed`` — the envelope carries the malformed/partial marker.
    # ``expect_stale`` — a row fell back to list-time data.
    # ``expect_code`` — a warning Code that MUST be present (or None).
    @pytest.mark.parametrize(
        "label,overrides,container_kept,expect_malformed,expect_stale,expect_code",
        [
            (
                "non-dict row next to a valid row",
                {
                    "list_container_fleets": [{"ContainerFleets": [_GOOD_CONTAINER, "not-a-mapping"]}],
                    "describe_container_fleet": lambda **kw: {"ContainerFleet": _GOOD_CONTAINER},
                },
                True,
                True,
                False,
                None,
            ),
            (
                "ContainerFleet null",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {"ContainerFleet": None},
                },
                False,
                True,
                True,
                "stale_list_time_data",
            ),
            (
                "DeploymentDetails null",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {
                        "ContainerFleet": {**_GOOD_CONTAINER, "DeploymentDetails": None}
                    },
                },
                True,
                False,
                False,
                None,
            ),
            (
                "LogConfiguration wrong type",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {
                        "ContainerFleet": {**_GOOD_CONTAINER, "LogConfiguration": "CLOUDWATCH"}
                    },
                },
                True,
                False,
                False,
                None,
            ),
            (
                "GameServerContainerGroupDefinitionArn non-string",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {
                        "ContainerFleet": {**_GOOD_CONTAINER, "GameServerContainerGroupDefinitionArn": 7}
                    },
                },
                True,
                False,
                False,
                None,
            ),
            (
                "GameServerContainerGroupDefinitionName unhashable",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {
                        "ContainerFleet": {**_GOOD_CONTAINER, "GameServerContainerGroupDefinitionName": ["x"]}
                    },
                },
                True,
                False,
                False,
                None,
            ),
            (
                "deployment item non-dict",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {
                        "ContainerFleet": {**_GOOD_CONTAINER, "DeploymentDetails": {"LatestDeploymentId": "d-1"}}
                    },
                    "list_fleet_deployments": [{"FleetDeployments": ["not-a-mapping"]}],
                },
                True,
                False,
                False,
                None,
            ),
            (
                "list_fleets FleetIds item unhashable while container IDs exist",
                {
                    "list_container_fleets": [{"ContainerFleets": [{"FleetId": "cf-good"}]}],
                    "describe_container_fleet": lambda **kw: {"ContainerFleet": _GOOD_CONTAINER},
                    "list_fleets": [{"FleetIds": ["fleet-classic-0001", {"nested": "blob"}]}],
                },
                True,
                True,
                False,
                None,
            ),
        ],
    )
    def test_malformed_container_shape_does_not_escape(
        self, label, overrides, container_kept, expect_malformed, expect_stale, expect_code
    ):
        cfg = {**_container_base_cfg(), **overrides}
        # Must NOT raise; the tool returns a sanitized envelope.
        result = _run(RecordingGameLift(**cfg))
        assert isinstance(result, dict), f"{label}: tool did not return an envelope"
        assert "status" in result
        # Already-fetched classic rows are not lost.
        assert result["ClassicFleets"] == [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}], label
        # The named container shape is actually reached (the lambdas close over
        # the module-scoped _GOOD_CONTAINER, so none raises NameError into the
        # describe-failure fallback), so the container-side outcome is pinned.
        if container_kept:
            assert result["ContainerFleets"], f"{label}: expected a container row to survive"
        else:
            assert result["ContainerFleets"] == [], f"{label}: expected the container row to be discarded"
        if expect_malformed:
            assert result.get("partial") is True, f"{label}: expected a partial/malformed envelope"
            assert result["status"] == "incomplete", f"{label}: expected incomplete status"
        if expect_stale:
            assert result.get("stale") is True, f"{label}: expected a list-time fallback (stale)"
        else:
            assert "stale" not in result, f"{label}: did not expect a stale fallback"
        if expect_code is not None:
            codes = {w.get("Code") for w in result["Warnings"]}
            assert expect_code in codes, f"{label}: expected warning code {expect_code!r}, got {codes}"
        _assert_no_sensitive(result)

    def test_unexpected_exception_in_helper_hits_safety_net(self):
        # Force an unexpected (non-provider) exception INSIDE the per-fleet
        # helper and prove the safety net keeps classic rows, records
        # malformed_response (a LOCAL fault, not provider_error), does not
        # re-raise, and does not leak. Patching the projection to raise a plain
        # RuntimeError exercises the ``except Exception`` safety net around
        # _summarize_one_container_fleet.
        # Local modules
        import agents.gamelift_specialist as gls

        cfg = {
            **_container_base_cfg(),
            "list_container_fleets": [{"ContainerFleets": [_GOOD_CONTAINER]}],
            "describe_container_fleet": lambda **kw: {"ContainerFleet": _GOOD_CONTAINER},
        }
        fake = RecordingGameLift(**cfg)
        with patch.object(gls, "project_container_fleet_summary", side_effect=RuntimeError("synthetic helper fault")):
            result = _run(fake)
        assert isinstance(result, dict)
        # Classic rows survive the container-side fault.
        assert result["ClassicFleets"] == [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        assert result.get("partial") is True
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes, f"safety net must record a LOCAL fault as malformed_response: {codes}"
        assert "provider_error" not in codes, "a local helper fault must not be labeled provider_error"
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# Typed error code when every listing call failed without a double denial.
# ---------------------------------------------------------------------------
class TestTotalFailureErrorCode:
    def test_both_throttled_pins_shared_error_code(self):
        throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "ListFleets")
        result = _run(RecordingGameLift(list_fleets=throttled, list_container_fleets=throttled))
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "throttled"
        _assert_no_sensitive(result)

    def test_mixed_failures_pin_provider_error(self):
        denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListFleets")
        throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "ListFleets")
        result = _run(RecordingGameLift(list_fleets=denied, list_container_fleets=throttled))
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "provider_error"


# ---------------------------------------------------------------------------
# Terminal malformed listing carries no truncation marker.
# ---------------------------------------------------------------------------
class TestTerminalMalformedHasNoTruncationMarker:
    def test_build_result_terminal_malformed_no_truncated(self):
        # Local modules
        from agents.gamelift_projections import build_fleet_list_result

        result = build_fleet_list_result(
            classic_rows=[],
            container_rows=[],
            warnings=[],
            malformed=True,
        )
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response"
        # No rows retained -> no truncation marker.
        assert "truncated" not in result

    def test_build_result_mixed_malformed_keeps_truncated(self):
        # Local modules
        from agents.gamelift_projections import build_fleet_list_result

        result = build_fleet_list_result(
            classic_rows=[{"FleetId": "fleet-aaaa-0001", "Status": "ACTIVE"}],
            container_rows=[],
            warnings=[],
            malformed=True,
        )
        assert result["status"] == "incomplete"
        assert result.get("truncated") is True


# ---------------------------------------------------------------------------
# Warning distinct-entry cap + overflow Count is total folded occurrences.
# ---------------------------------------------------------------------------
class TestWarningCapAndOverflowCount:
    def test_more_than_cap_distinct_keys_fold_to_overflow(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_WARNINGS, _dedupe_warnings

        n = GAMELIFT_MAX_WARNINGS + 5
        occurrences = 3
        warnings = [{"Source": f"s{i}", "Code": "c"} for i in range(n) for _ in range(occurrences)]
        deduped = _dedupe_warnings(warnings)
        assert len(deduped) == GAMELIFT_MAX_WARNINGS
        overflow = deduped[-1]
        assert overflow["Source"] == "warnings"
        assert overflow["Code"] == "additional_warnings_truncated"
        # Overflow Count is the TOTAL folded occurrences, not the distinct-key count.
        folded_distinct = n - (GAMELIFT_MAX_WARNINGS - 1)
        assert overflow["Count"] == folded_distinct * occurrences


# ---------------------------------------------------------------------------
# Operation-set guard catches an unexpected call inside a try block.
# ---------------------------------------------------------------------------
class TestOperationSetProxyGuard:
    ALLOWED = {
        "list_fleets",
        "describe_fleet_attributes",
        "list_container_fleets",
        "list_container_group_definitions",
        "describe_container_fleet",
        "describe_container_group_definition",
        "list_fleet_deployments",
        "describe_fleet_utilization",
        "describe_fleet_capacity",
        "describe_scaling_policies",
    }

    def test_recorded_operations_subset_of_allowed_set_failure_paths(self):
        # Drive the failure + per-ID fallback paths so every operation the tool
        # touches is recorded through __getattr__.
        denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Op")
        fake = RecordingGameLift(
            list_fleets=[{"FleetIds": ["fleet-aaaa-0001", "fleet-bbbb-0002"]}],
            # First describe fails for the chunk, forcing per-ID fallback.
            describe_fleet_attributes=_chunk_then_per_id(),
            list_container_fleets=[{"ContainerFleets": [{"FleetId": "cf-1"}]}],
            describe_container_fleet=denied,
        )
        _run(fake)
        touched = {op for op in fake.invoked_operations if op != "get_paginator"}
        assert touched, "no operations recorded"
        unexpected = touched - self.ALLOWED - {"gamelift"}
        assert not unexpected, f"tool touched unexpected operations: {unexpected}"


def _chunk_then_per_id():
    state = {"calls": 0}

    def describe(**kw: Any):
        state["calls"] += 1
        ids = kw["FleetIds"]
        if len(ids) > 1:
            raise ClientError({"Error": {"Code": "InvalidRequestException", "Message": "bad"}}, "Describe")
        return {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in ids]}

    return describe


# ---------------------------------------------------------------------------
# stale is set for both list-time fallbacks; add paginated assertion.
# ---------------------------------------------------------------------------
class TestStaleSemantics:
    def test_describe_container_fleet_missing_container_fleet_sets_stale(self):
        listed = {"FleetId": "cf-1", "Status": "ACTIVE", "InstanceType": "c6i.large"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet={},  # missing ContainerFleet -> list-time fallback
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result.get("stale") is True, "missing ContainerFleet fallback must set stale"
        # A warning records the fallback.
        assert any(w["Source"] in {"container_fleet"} for w in result["Warnings"])

    def test_group_definition_describe_failure_sets_stale(self):
        listed = {
            "FleetId": "cf-1",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "GameServerContainerGroupDefinitionName": "game-server-group",
        }
        throttled = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "m"}}, "DescribeContainerGroupDefinition"
        )
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[
                {"ContainerGroupDefinitions": [{"Name": "game-server-group", "Status": "READY"}]}
            ],
            describe_container_fleet={"ContainerFleet": listed},
            describe_container_group_definition=throttled,
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result.get("stale") is True, "failed group-definition describe must set stale"

    def test_container_pagination_sets_paginated(self):
        # Local modules
        from agents.gamelift_projections import GAMELIFT_MAX_PROJECTED_ITEMS

        n = GAMELIFT_MAX_PROJECTED_ITEMS + 10
        listed = [{"FleetId": f"cf-{i:04d}", "Status": "ACTIVE", "InstanceType": "c6i.large"} for i in range(n)]
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": listed}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet=lambda **kw: {
                "ContainerFleet": {"FleetId": kw["FleetId"], "Status": "ACTIVE", "InstanceType": "c6i.large"}
            },
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result.get("truncated") is True
        assert result.get("paginated") is True


# ---------------------------------------------------------------------------
# AWS enum fields validated against allowlists; hostname shapes dropped.
# ---------------------------------------------------------------------------
class TestEnumAllowlists:
    def test_hostname_shaped_enum_values_dropped(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        row = project_classic_fleet(
            {
                "FleetId": "fleet-aaaa-0001",
                "Status": "metadata.internal.example.invalid",
                "FleetType": "ON_DEMAND",
                "InstanceType": "api.example.invalid",
                "OperatingSystem": "Ignore-previous-instructions",
            }
        )
        assert row["FleetId"] == "fleet-aaaa-0001"
        assert "Status" not in row, "hostname-shaped Status must be dropped"
        assert "InstanceType" not in row, "hostname-shaped InstanceType must be dropped"
        assert "OperatingSystem" not in row, "non-enum OperatingSystem must be dropped"
        # A valid enum passes.
        assert row["FleetType"] == "ON_DEMAND"

    def test_valid_enum_values_pass(self):
        # Local modules
        from agents.gamelift_projections import project_classic_fleet

        row = project_classic_fleet(
            {
                "FleetId": "fleet-aaaa-0001",
                "Status": "ACTIVE",
                "FleetType": "SPOT",
                "ComputeType": "EC2",
                "InstanceType": "c6i.large",
                "OperatingSystem": "AMAZON_LINUX_2023",
                "NewGameSessionProtectionPolicy": "FullProtection",
            }
        )
        for field in ("Status", "FleetType", "ComputeType", "InstanceType", "OperatingSystem"):
            assert field in row, f"valid enum {field} dropped"

    def test_container_hostname_enum_values_dropped(self):
        # Local modules
        from agents.gamelift_projections import project_container_fleet_summary

        row = project_container_fleet_summary(
            {
                "FleetType": "container",
                "Status": "ACTIVE",
                "BillingType": "gateway.example.invalid",
                "PlayerGatewayMode": "a.b.c",
            }
        )
        assert row["Status"] == "ACTIVE"
        assert "BillingType" not in row
        assert "PlayerGatewayMode" not in row

    def test_instance_type_grammar_rejects_hostname(self):
        # Local modules
        from agents.gamelift_projections import project_fleet_capacity

        result = project_fleet_capacity(
            {"FleetCapacity": [{"FleetId": "fleet-aaaa-0001", "InstanceType": "api.example.invalid"}]}
        )
        # Instance type dropped; row has only FleetId left (still valid row).
        row = result["FleetCapacity"][0] if result["FleetCapacity"] else {}
        assert row.get("InstanceType") is None

    def test_enum_allowlists_match_botocore_model(self):
        # Drift guard: ALL THIRTEEN hard-coded enum allowlists equal the pinned
        # model. Iterate one table of allowlist-attribute -> (shape, member) so a
        # new allowlist cannot be silently omitted from the comparison.
        # Third-party packages
        import botocore.session

        # Local modules
        from agents import gamelift_projections as glp

        model = botocore.session.get_session().get_service_model("gamelift")

        def model_enum(shape_name: str, member: str) -> set[str]:
            shape = model.shape_for(shape_name)
            return set(shape.members[member].enum)

        pairs = {
            "_FLEET_STATUS_ENUM": ("FleetAttributes", "Status"),
            "_FLEET_TYPE_ENUM": ("FleetAttributes", "FleetType"),
            "_COMPUTE_TYPE_ENUM": ("FleetAttributes", "ComputeType"),
            "_OPERATING_SYSTEM_ENUM": ("FleetAttributes", "OperatingSystem"),
            "_PROTECTION_POLICY_ENUM": ("FleetAttributes", "NewGameSessionProtectionPolicy"),
            "_CONTAINER_FLEET_STATUS_ENUM": ("ContainerFleet", "Status"),
            "_CONTAINER_BILLING_TYPE_ENUM": ("ContainerFleet", "BillingType"),
            "_PLAYER_GATEWAY_MODE_ENUM": ("ContainerFleet", "PlayerGatewayMode"),
            "_CONTAINER_GROUP_TYPE_ENUM": ("ContainerGroupDefinition", "ContainerGroupType"),
            "_CONTAINER_GROUP_STATUS_ENUM": ("ContainerGroupDefinition", "Status"),
            "_CONTAINER_OS_ENUM": ("ContainerGroupDefinition", "OperatingSystem"),
            "_DEPLOYMENT_STATUS_ENUM": ("FleetDeployment", "DeploymentStatus"),
            "_LOG_DESTINATION_ENUM": ("LogConfiguration", "LogDestination"),
        }
        # Every allowlist constant in the module is covered by the table above.
        allowlist_consts = {name for name in vars(glp) if name.endswith("_ENUM")}
        assert allowlist_consts == set(pairs), f"untested enum allowlists: {allowlist_consts ^ set(pairs)}"
        for const, (shape_name, member) in pairs.items():
            assert set(getattr(glp, const)) == model_enum(shape_name, member), f"{const} drifted from the pinned model"


# ---------------------------------------------------------------------------
# Wrong-typed describe_fleet_attributes response is not amplified.
# ---------------------------------------------------------------------------
class TestClassicDescribeShapeCheck:
    def test_null_fleet_attributes_not_amplified_into_per_id_fallback(self):
        # 3 listed IDs; a null FleetAttributes response must be treated as a
        # malformed shape, NOT re-tried per ID (which would be 1 + N calls).
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": []}],
            list_fleets=[{"FleetIds": ["fleet-0001", "fleet-0002", "fleet-0003"]}],
            describe_fleet_attributes={"FleetAttributes": None},
        )
        result = _run(fake)
        describe_calls = sum(1 for op in fake.invoked_operations if op == "describe_fleet_attributes")
        assert describe_calls == 1, f"wrong-typed response amplified into per-ID fallback: {describe_calls} calls"
        assert result["ClassicFleets"] == []
        # Malformed shape surfaces as incomplete, not a clean empty.
        assert result["status"] == "incomplete"

    def test_missing_fleet_attributes_key_is_empty_not_malformed(self):
        # An ABSENT FleetAttributes key means empty, not malformed.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": []}],
            list_fleets=[{"FleetIds": ["fleet-0001"]}],
            describe_fleet_attributes={},
        )
        result = _run(fake)
        # No classic rows, a clean empty status, and no error key from the
        # absent describe (an absent key is empty, never malformed).
        assert result["ClassicFleets"] == []
        assert result["status"] == "empty"
        assert "error" not in result


# ---------------------------------------------------------------------------
# list_fleet_deployments bounded to the latest deployment only.
# ---------------------------------------------------------------------------
class TestDeploymentReadBounded:
    def test_deployment_paginate_uses_small_maxitems(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        captured: dict[str, Any] = {}

        class _Fake:
            def get_paginator(self, operation_name: str) -> MagicMock:
                pag = MagicMock()
                if operation_name == "list_container_fleets":
                    pag.paginate.return_value = [{"ContainerFleets": [{"FleetId": "cf-1"}]}]
                elif operation_name == "list_fleet_deployments":

                    def paginate(**kwargs: Any):
                        captured["deployments"] = kwargs
                        return [{"FleetDeployments": []}]

                    pag.paginate.side_effect = paginate
                elif operation_name == "list_container_group_definitions":
                    pag.paginate.return_value = [{"ContainerGroupDefinitions": []}]
                else:
                    pag.paginate.return_value = [{"FleetIds": []}]
                return pag

            def describe_container_fleet(self, **kw: Any):
                return {"ContainerFleet": {"FleetId": kw["FleetId"], "Status": "ACTIVE", "InstanceType": "c6i.large"}}

        with patch("agents.gamelift_specialist.boto3.client", return_value=_Fake()):
            list_gamelift_fleets()

        assert "deployments" in captured, "list_fleet_deployments was not paginated"
        pconfig = captured["deployments"].get("PaginationConfig", {})
        assert pconfig.get("MaxItems") == 1, "deployment read should be bounded to the latest deployment only"


# ---------------------------------------------------------------------------
# Instance-type grammar: anchored size token, dashed families, model-tied.
# ---------------------------------------------------------------------------
class TestInstanceTypeGrammar:
    def test_instance_type_grammar_matches_every_pinned_ec2_type(self):
        # Drift guard: every value in the pinned botocore EC2InstanceType enum
        # must match the instance-type grammar, so a legitimate instance type is
        # never dropped from a projected row.
        # Third-party packages
        import botocore.session

        # Local modules
        from agents import gamelift_projections as glp

        model = botocore.session.get_session().get_service_model("gamelift")
        ec2_types = set(model.shape_for("FleetAttributes").members["InstanceType"].enum)
        assert ec2_types, "pinned model exposes no EC2InstanceType enum"
        rejected = sorted(v for v in ec2_types if not glp._valid_typed_string(v, glp._INSTANCE_TYPE))
        assert rejected == [], f"pinned EC2 instance types rejected by the grammar: {rejected}"

    def test_dashed_families_and_size_tokens_accepted(self):
        # Local modules
        from agents import gamelift_projections as glp

        for good in ("c6i.large", "m5.24xlarge", "c7i-flex.large", "u7i-12tb.224xlarge", "c6i.metal", "r7i.metal-24xl"):
            assert glp._valid_typed_string(good, glp._INSTANCE_TYPE), f"{good} should be accepted"

    def test_hostname_shaped_instance_types_rejected(self):
        # A two-label host shape (``metadata.internal``) and other non-size
        # second labels must fail: the size token is anchored to real EC2 sizes.
        # The family token also allows at most one dash group, bounds each
        # token's length, and requires a digit in the family, so a dash-encoded
        # IPv4 label, a digit-free family, and an oversized family are rejected.
        # Local modules
        from agents import gamelift_projections as glp

        host = "metadata." + "internal"
        dashed_ipv4 = "ip-10-0-0-1.large"  # four dash groups, dash-encoded IPv4
        digitless_family = "metadata.large"  # family carries no digit
        oversized_family = "c1" + "a" * 20 + ".large"  # family token past the length bound
        for bad in (
            host,
            "api.example",
            "c6i.largex",
            "c6i.",
            ".large",
            "c6i.large.extra",
            dashed_ipv4,
            digitless_family,
            oversized_family,
        ):
            assert not glp._valid_typed_string(bad, glp._INSTANCE_TYPE), f"{bad} should be rejected"

    def test_capacity_tool_keeps_valid_instance_type_rejects_hostname(self):
        # The capacity detail tool uses the same grammar: a real type survives,
        # a hostname-shaped value is dropped from the row.
        # Local modules
        from agents.gamelift_projections import project_fleet_capacity

        ok = project_fleet_capacity(
            {"FleetCapacity": [{"FleetId": "fleet-aaaa-0001", "InstanceType": "c7i-flex.large"}]}
        )
        assert ok["FleetCapacity"][0].get("InstanceType") == "c7i-flex.large"
        host = "metadata." + "internal"
        bad = project_fleet_capacity({"FleetCapacity": [{"FleetId": "fleet-aaaa-0001", "InstanceType": host}]})
        assert bad["FleetCapacity"][0].get("InstanceType") is None


# ---------------------------------------------------------------------------
# Fleet-ID exemption exactness: anchor, prefixes, case, and 128-char bound,
# plus the detail-tool FleetId change (canonical kept, hostile 12-digit dropped).
# ---------------------------------------------------------------------------
class TestFleetIdExemptionExactness:
    def _digits12(self) -> str:
        return "".join(str((i * 7) % 10) for i in range(12))

    def test_only_exact_canonical_digit_tail_is_exempt(self):
        # Local modules
        from agents.gamelift_projections import _FLEET_ID, _valid_typed_string

        d12 = self._digits12()
        uuid_tail = "2222bbbb-33cc-44dd-55ee-" + d12
        assert _valid_typed_string("fleet-" + uuid_tail, _FLEET_ID)
        assert _valid_typed_string("containerfleet-" + uuid_tail, _FLEET_ID)
        # Non-canonical shapes carrying a 12-digit run stay rejected (pins the
        # end anchor, the exact fleet-/containerfleet- prefix, and lowercase hex).
        rejected = [
            "fleet-" + uuid_tail + "-x",  # trailing suffix (end anchor)
            "fleet-" + uuid_tail + d12[:1],  # 13-char tail
            "anywherefleet-" + uuid_tail,  # other lowercase prefix
            "fleet-2222BBBB-33cc-44dd-55ee-" + d12,  # uppercase hex group
            "fleet-" + d12,  # fleet- + bare 12 digits
            d12,  # bare 12 digits
        ]
        for value in rejected:
            assert not _valid_typed_string(value, _FLEET_ID), f"{value!r} must stay rejected"

    def test_canonical_digit_tail_rejected_outside_fleet_id_kind(self):
        # The exemption lives ONLY in the fleet-ID kind; identifier/name/token
        # must still reject the canonical digit-tailed value.
        # Local modules
        from agents.gamelift_projections import _IDENTIFIER, _NAME, _TOKEN, _valid_typed_string

        value = "fleet-2222bbbb-33cc-44dd-55ee-" + self._digits12()
        for kind in (_IDENTIFIER, _NAME, _TOKEN):
            assert not _valid_typed_string(value, kind), f"kind {kind} must reject the canonical digit tail"

    def test_fleet_id_bounded_at_model_maximum_128(self):
        # The model caps FleetId at 128 characters. The length bound is enforced
        # independently of the suffix grammar: a value of exactly 128 characters
        # is accepted and 129 is rejected. Use a long lowercase prefix (matched by
        # the leading ``[a-z]*`` of the prefix grammar) so the suffix-length limit
        # does not reject the value first, isolating the 128-character bound.
        # Local modules
        from agents.gamelift_projections import _FLEET_ID, GAMELIFT_MAX_FLEET_ID_LENGTH, _valid_typed_string

        assert GAMELIFT_MAX_FLEET_ID_LENGTH == 128
        # A canonical id well within the bound is accepted.
        assert _valid_typed_string("fleet-2222bbbb-33cc-44dd-55ee-6666ffff77aa", _FLEET_ID)
        # Exactly 128 characters (121 lowercase prefix + "fleet-" + 1) is accepted.
        at_bound = "a" * 121 + "fleet-" + "b"
        assert len(at_bound) == GAMELIFT_MAX_FLEET_ID_LENGTH
        assert _valid_typed_string(at_bound, _FLEET_ID)
        # One character past the model maximum (129) is rejected only by the
        # length bound; its suffix still satisfies the prefix grammar.
        too_long = "a" * 122 + "fleet-" + "b"
        assert len(too_long) == GAMELIFT_MAX_FLEET_ID_LENGTH + 1
        assert not _valid_typed_string(too_long, _FLEET_ID)

    def test_detail_tools_keep_canonical_drop_hostile_fleet_id(self):
        # Utilization, capacity, and scaling all use the fleet-ID grammar: a
        # canonical digit-tailed id survives; a padded 12-digit value is dropped
        # from the row (FleetId omitted).
        # Local modules
        from agents.gamelift_projections import (
            project_fleet_capacity,
            project_fleet_utilization,
            project_scaling_policies,
        )

        d12 = self._digits12()
        good = "fleet-2222bbbb-33cc-44dd-55ee-" + d12
        hostile = "fleet-prod-" + d12  # digit run, not a canonical UUID

        util = project_fleet_utilization(
            {"FleetUtilization": [{"FleetId": good, "ActiveServerProcessCount": 1, "Location": "us-west-2"}]}
        )
        assert util["FleetUtilization"][0]["FleetId"] == good
        cap = project_fleet_capacity({"FleetCapacity": [{"FleetId": good, "InstanceType": "c6i.large"}]})
        assert cap["FleetCapacity"][0]["FleetId"] == good
        scaling = project_scaling_policies({"ScalingPolicies": [{"FleetId": good, "Name": "p1"}]})
        assert scaling["ScalingPolicies"][0]["FleetId"] == good

        # Hostile padded 12-digit FleetId -> the FleetId is dropped from the row
        # in every detail tool (the row retains only its other valid fields).
        for projector, key in (
            (project_fleet_utilization, "FleetUtilization"),
            (project_fleet_capacity, "FleetCapacity"),
            (project_scaling_policies, "ScalingPolicies"),
        ):
            out = projector({key: [{"FleetId": hostile, "Location": "us-west-2"}]})
            row = out[key][0] if out[key] else {}
            assert row.get("FleetId") is None, f"{key}: hostile 12-digit FleetId must be dropped"


# ---------------------------------------------------------------------------
# Wrong-typed list collections: malformed, never iterated; absent is empty.
# ---------------------------------------------------------------------------
class TestListPageShapeChecks:
    @pytest.mark.parametrize(
        "fleet_ids_value",
        [None, 7, "fleet-abc", {"FleetId": "fleet-abc"}],
    )
    def test_wrong_typed_fleet_ids_is_malformed_not_iterated(self, fleet_ids_value):
        # A present-but-wrong-typed FleetIds (null/number/string/mapping) is a
        # malformed shape: no describe_fleet_attributes call, no iteration into
        # characters or keys, and the envelope is partial.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": []}],
            list_fleets=[{"FleetIds": fleet_ids_value}],
        )
        result = _run(fake)
        describe_calls = sum(1 for op in fake.invoked_operations if op == "describe_fleet_attributes")
        assert describe_calls == 0, f"wrong-typed FleetIds iterated into {describe_calls} describe calls"
        assert result["ClassicFleets"] == []
        assert result["status"] == "incomplete"
        assert result.get("partial") is True
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    def test_absent_fleet_ids_key_is_empty(self):
        # An ABSENT FleetIds key is a valid empty page, not malformed.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": []}],
            list_fleets=[{}],
        )
        result = _run(fake)
        assert result["ClassicFleets"] == []
        assert result["status"] == "empty"
        assert "error" not in result

    def test_wrong_typed_container_fleets_is_malformed(self):
        # A present-but-wrong-typed ContainerFleets collection is malformed too.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": "cf-abc"}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    @pytest.mark.parametrize("container_fleets_value", [None, 7, {"FleetId": "cf-abc"}])
    def test_wrong_typed_container_fleets_shapes_are_malformed(self, container_fleets_value):
        # Null, numeric, and mapping ContainerFleets collections are each a
        # present-but-wrong-typed shape: never iterated, always malformed.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": container_fleets_value}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    def test_non_mapping_list_fleets_page_is_malformed(self):
        # A non-mapping PAGE from the classic list path (not a dict) is a
        # malformed shape: it contributes no IDs, triggers no describe call, and
        # the envelope is partial with a malformed warning.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": []}],
            list_fleets=["not-a-mapping-page"],
        )
        result = _run(fake)
        describe_calls = sum(1 for op in fake.invoked_operations if op == "describe_fleet_attributes")
        assert describe_calls == 0, f"non-mapping page iterated into {describe_calls} describe calls"
        assert result["ClassicFleets"] == []
        assert result["status"] == "incomplete"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    def test_non_mapping_container_fleets_page_is_malformed(self):
        # A non-mapping PAGE from the container list path is likewise malformed.
        fake = RecordingGameLift(
            list_container_fleets=["not-a-mapping-page"],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    def test_mixed_classic_rows_kept_while_container_malformed(self):
        # A wrong-typed ContainerFleets collection discards the container view,
        # but a valid classic row is retained: the envelope keeps the classic
        # row while remaining partial, malformed, and truncated.
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": "cf-abc"}],
            list_fleets=[{"FleetIds": ["fleet-classic-0001"]}],
            describe_fleet_attributes={"FleetAttributes": [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]},
        )
        result = _run(fake)
        assert result["ClassicFleets"] == [{"FleetId": "fleet-classic-0001", "Status": "ACTIVE"}]
        assert result["status"] == "incomplete"
        assert result.get("partial") is True
        assert result.get("truncated") is True
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes

    def test_mixed_container_rows_kept_while_classic_malformed(self):
        # A wrong-typed FleetIds collection discards the classic view, but a
        # valid container row is retained: the envelope keeps the container row
        # while remaining partial, malformed, and truncated.
        good = {"FleetId": "cf-good", "Status": "ACTIVE", "InstanceType": "c6i.large"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [good]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": good},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": "fleet-abc"}],
        )
        result = _run(fake)
        assert result["ContainerFleets"], "valid container row must be retained"
        assert result["status"] == "incomplete"
        assert result.get("partial") is True
        assert result.get("truncated") is True
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes


# ---------------------------------------------------------------------------
# Classic paging ceiling counts classic IDs after container exclusion.
# ---------------------------------------------------------------------------
class TestClassicPagingCeiling:
    def _canonical_classic_ids(self, n: int) -> list[str]:
        return [f"fleet-{i:08x}-aaaa-4bbb-8ccc-{i:012d}" for i in range(n)]

    def test_60_classic_plus_50_container_returns_all_60_complete(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        container_ids = [f"containerfleet-0000aaaa-1111-4bbb-8ccc-{i:012d}" for i in range(50)]
        classic_ids = self._canonical_classic_ids(60)

        stub.add_response(
            "list_container_fleets",
            {
                "ContainerFleets": [
                    {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"} for cid in container_ids
                ]
            },
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        for cid in container_ids:
            stub.add_response(
                "describe_container_fleet",
                {"ContainerFleet": {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
            )
            stub.add_response("list_fleet_deployments", {"FleetDeployments": []})

        # Interleave container and classic IDs across two pages; the ceiling
        # (100 + 50 + 1 = 151) is large enough to read all 110 raw IDs.
        interleaved: list[str] = []
        for i in range(60):
            interleaved.append(classic_ids[i])
            interleaved.append(container_ids[i % 50])
        stub.add_response("list_fleets", {"FleetIds": interleaved[:80], "NextToken": "p2"})
        stub.add_response("list_fleets", {"FleetIds": interleaved[80:]})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in classic_ids]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        # All 60 classic fleets returned and the classic view is COMPLETE: the
        # container IDs sharing the stream must not shrink or truncate it.
        assert result["FleetCounts"]["Classic"] == 60, result["FleetCounts"]
        assert "truncated" not in result or result.get("truncated") is not True
        assert result.get("paginated") is not True
        assert result["status"] != "incomplete" or not any(
            w.get("Source") == "classic_fleets" for w in result["Warnings"]
        )

    def test_100_classic_plus_1_container_is_complete_not_paginated(self):
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        container_id = "containerfleet-0000aaaa-1111-4bbb-8ccc-" + ("0" * 11 + "1")
        classic_ids = self._canonical_classic_ids(100)

        stub.add_response(
            "list_container_fleets",
            {"ContainerFleets": [{"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}]},
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        stub.add_response(
            "describe_container_fleet",
            {"ContainerFleet": {"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
        )
        stub.add_response("list_fleet_deployments", {"FleetDeployments": []})
        # 101 raw IDs in ONE page, no NextToken -> complete; ceiling is 102.
        stub.add_response("list_fleets", {"FleetIds": [container_id] + classic_ids})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in classic_ids]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        assert result["FleetCounts"]["Classic"] == 100, result["FleetCounts"]
        # Complete: no residual pagination markers for a fully-returned classic set.
        assert result.get("paginated") is not True, "complete 100-classic set wrongly marked paginated"

    def test_ceiling_stopped_paginator_with_resume_token_is_paginated(self):
        # Even when the CLASSIC count read is at or below the cap, if the raw
        # ceiling stopped a paginator that still had a resume token, more fleets
        # (possibly classic) existed beyond what was read: the view must be
        # marked truncated/paginated, never complete.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        # 100 container fleets listed -> excluded_count 100 -> raw ceiling 201.
        container_ids = [f"containerfleet-0000aaaa-1111-4bbb-8ccc-{i:012d}" for i in range(100)]
        classic_ids = self._canonical_classic_ids(30)

        stub.add_response(
            "list_container_fleets",
            {
                "ContainerFleets": [
                    {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"} for cid in container_ids
                ]
            },
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        for cid in container_ids:
            stub.add_response(
                "describe_container_fleet",
                {"ContainerFleet": {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
            )
            stub.add_response("list_fleet_deployments", {"FleetDeployments": []})

        # Raw stream: a single page of 210 IDs (30 classic + 180 container)
        # carrying a NextToken. The ceiling (201) truncates the page and stops
        # the paginator with a resume token still present, even though only 30
        # classic IDs were read.
        first_page = classic_ids + container_ids[:90] + container_ids[:90]
        stub.add_response("list_fleets", {"FleetIds": first_page, "NextToken": "more"})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in classic_ids]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        assert result["FleetCounts"]["Classic"] == 30, result["FleetCounts"]
        # A resume token remained, so the classic view is NOT complete.
        assert result.get("truncated") is True, "ceiling-stopped paginator with resume token must mark truncated"
        assert result.get("paginated") is True, "a remaining resume token must mark paginated"

    def test_repeated_container_ids_fill_ceiling_without_resume_token_marks_truncated(self):
        # A final page without a NextToken whose repeated container IDs fill the
        # raw ceiling can hide classic IDs beyond the ceiling. Reaching the raw
        # ceiling must therefore mark the classic view truncated even without a
        # resume token, so the hidden classic IDs are never read as complete.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        # 100 container fleets listed -> excluded_count 100 -> raw ceiling 201.
        container_ids = [f"containerfleet-0000aaaa-1111-4bbb-8ccc-{i:012d}" for i in range(100)]
        classic_ids = self._canonical_classic_ids(35)

        stub.add_response(
            "list_container_fleets",
            {
                "ContainerFleets": [
                    {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"} for cid in container_ids
                ]
            },
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        for cid in container_ids:
            stub.add_response(
                "describe_container_fleet",
                {"ContainerFleet": {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
            )
            stub.add_response("list_fleet_deployments", {"FleetDeployments": []})

        # One page of 206 IDs with NO NextToken: 30 classic, 100 container, 71
        # repeated container, then 5 more classic. The ceiling (201) stops the
        # read after the repeats, so only 30 classic IDs are seen and the final
        # 5 classic IDs are hidden. With no resume token, reaching the ceiling is
        # the only signal that classic IDs may remain.
        single_page = classic_ids[:30] + container_ids + container_ids[:71] + classic_ids[30:35]
        stub.add_response("list_fleets", {"FleetIds": single_page})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in classic_ids[:30]]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        # Only 30 of 35 classic fleets were seen, so the view must not read ok.
        assert result["status"] != "ok", "hidden classic residual read as a complete ok inventory"
        assert result.get("truncated") is True, "reaching the raw ceiling must mark the classic view truncated"

    def test_container_fleet_beyond_container_cap_not_listed_as_classic(self):
        # The container listing is item-capped at 100 rows, so the exclusion set
        # holds at most 100 container IDs. A 101st container fleet whose ID still
        # appears in list_fleets must NOT be described and classified as a classic
        # fleet: containerfleet-prefixed IDs are excluded from the classic path
        # independently of the exclusion-set size.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        container_ids = [f"containerfleet-0000aaaa-1111-4bbb-8ccc-{i:012d}" for i in range(101)]

        stub.add_response(
            "list_container_fleets",
            {
                "ContainerFleets": [
                    {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"} for cid in container_ids
                ]
            },
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        # Only the first 100 container fleets are described (item cap).
        for cid in container_ids[:100]:
            stub.add_response(
                "describe_container_fleet",
                {"ContainerFleet": {"FleetId": cid, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
            )
            stub.add_response("list_fleet_deployments", {"FleetDeployments": []})
        # list_fleets returns every container ID (including the 101st). The
        # exclusion SET holds only the first 100 container IDs (the listing is
        # item-capped), so without the ``containerfleet-`` prefix rule the 101st
        # ID would survive set exclusion and reach describe_fleet_attributes,
        # which this stub answers and classifies as a CLASSIC fleet. Dropping
        # every ``containerfleet-`` ID from the classic path keeps it off the
        # classic describe regardless of the exclusion-set size.
        stub.add_response("list_fleets", {"FleetIds": container_ids})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": container_ids[100], "Status": "ACTIVE"}]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        # No container fleet is ever classified as classic.
        assert result["FleetCounts"]["Classic"] == 0, "a container fleet was classified as a classic fleet"
        assert all(
            not row["FleetId"].startswith("containerfleet-")
            for row in result["ClassicFleets"]
            if isinstance(row.get("FleetId"), str)
        )

    def test_listed_container_absent_from_list_fleets_still_truncates_on_classic_count(self):
        # The classic-count clause carries weight independently of the raw
        # ceiling. 101 classic fleets exist and one listed container fleet is
        # ABSENT from list_fleets (e.g. deleted between the two reads), so the
        # raw read (101 IDs) stays BELOW the ceiling (100 + 1 excluded + 1 =
        # 102). Truncating only on the raw ceiling would read this 100-of-101
        # view as a clean ok; the classic-count clause (101 classic > 100 cap)
        # must mark it truncated instead.
        # Local modules
        from agents.gamelift_specialist import list_gamelift_fleets

        client, stub = _stub_client()
        container_id = "containerfleet-0000aaaa-1111-4bbb-8ccc-0000000000a1"
        classic_ids = self._canonical_classic_ids(101)

        stub.add_response(
            "list_container_fleets",
            {"ContainerFleets": [{"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}]},
        )
        stub.add_response("list_container_group_definitions", {"ContainerGroupDefinitions": []})
        stub.add_response(
            "describe_container_fleet",
            {"ContainerFleet": {"FleetId": container_id, "Status": "ACTIVE", "InstanceType": "c6i.large"}},
        )
        stub.add_response("list_fleet_deployments", {"FleetDeployments": []})
        # list_fleets returns 101 classic IDs only; the listed container fleet is
        # absent, so the raw read (101) is below the ceiling (102).
        stub.add_response("list_fleets", {"FleetIds": classic_ids})
        stub.add_response(
            "describe_fleet_attributes",
            {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in classic_ids[:100]]},
        )
        with stub, patch("agents.gamelift_specialist.boto3.client", return_value=client):
            result = list_gamelift_fleets()

        assert result["FleetCounts"]["Classic"] == 100, result["FleetCounts"]
        assert result["status"] != "ok", "101 classic fleets capped at 100 read as a complete ok inventory"
        assert result.get("truncated") is True, "the classic-count clause must mark 101>100 truncated"

    def test_throttled_container_listing_keeps_containerfleet_ids_off_classic_describe(self):
        # When list_container_fleets is throttled, the exclusion set is empty, so
        # a containerfleet- ID in list_fleets would survive SET exclusion. The
        # unconditional containerfleet- prefix rule must still keep it off
        # describe_fleet_attributes. Record every describe_fleet_attributes call
        # and assert no containerfleet- ID is ever passed, even with an empty
        # exclusion set.
        container_id = "containerfleet-0000aaaa-1111-4bbb-8ccc-0000000000a1"
        classic_id = "fleet-00000001-aaaa-4bbb-8ccc-0000000000a1"
        throttle = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListContainerFleets"
        )
        described_ids: list[list[str]] = []

        def _describe(**kw: Any) -> dict[str, Any]:
            ids = list(kw.get("FleetIds", []))
            described_ids.append(ids)
            return {"FleetAttributes": [{"FleetId": fid, "Status": "ACTIVE"} for fid in ids]}

        fake = RecordingGameLift(
            # Throttle the whole container listing -> empty exclusion set.
            list_container_fleets=throttle,
            list_fleets=[{"FleetIds": [container_id, classic_id]}],
            describe_fleet_attributes=_describe,
        )
        result = _run(fake)

        flat_ids = [fid for call in described_ids for fid in call]
        assert all(
            not fid.startswith("containerfleet-") for fid in flat_ids
        ), f"a containerfleet- ID reached describe_fleet_attributes: {flat_ids}"
        assert result["FleetCounts"]["Classic"] == 1, result["FleetCounts"]
        assert all(
            not row["FleetId"].startswith("containerfleet-")
            for row in result["ClassicFleets"]
            if isinstance(row.get("FleetId"), str)
        ), "a containerfleet- ID was classified as a classic fleet"


# ---------------------------------------------------------------------------
# Group-definition describe fallback: keep list-time def, set stale, no None.
# ---------------------------------------------------------------------------
class TestGroupDefinitionFallback:
    def test_missing_group_definition_keeps_list_time_def_sets_stale_two_fleets(self):
        # Two container fleets share one group definition. describe returns a
        # response WITHOUT a usable ContainerGroupDefinition: both rows keep the
        # list-time definition, the envelope is stale, a code-owned warning is
        # recorded, and None is never cached (so the second fleet still has it).
        listed = [
            {
                "FleetId": "cf-1",
                "Status": "ACTIVE",
                "InstanceType": "c6i.large",
                "GameServerContainerGroupDefinitionName": "game-server-group",
            },
            {
                "FleetId": "cf-2",
                "Status": "ACTIVE",
                "InstanceType": "c6i.large",
                "GameServerContainerGroupDefinitionName": "game-server-group",
            },
        ]
        list_time_def = {"Name": "game-server-group", "Status": "READY", "ContainerGroupType": "GAME_SERVER"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": listed}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": [list_time_def]}],
            describe_container_fleet=lambda **kw: {
                "ContainerFleet": next(f for f in listed if f["FleetId"] == kw["FleetId"])
            },
            # Missing ContainerGroupDefinition key -> fall back to list-time def.
            describe_container_group_definition=lambda **kw: {},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result.get("stale") is True, "missing ContainerGroupDefinition must set stale"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "stale_list_time_data" in codes
        # Both fleets kept the list-time group definition (None was never cached).
        assert result["FleetCounts"]["Container"] == 2
        for row in result["ContainerFleets"]:
            assert row.get("ContainerGroupDefinition", {}).get("Name") == "game-server-group"


# ---------------------------------------------------------------------------
# Sub-read malformed pages (group definitions, deployments) surface a warning.
# ---------------------------------------------------------------------------
class TestSubReadMalformedPages:
    @pytest.mark.parametrize("bad_collection", [None, 7, "defs", {"Name": "d"}])
    def test_malformed_group_definition_page_surfaces_warning(self, bad_collection):
        # A present-but-wrong-typed ContainerGroupDefinitions collection on the
        # group-definition list read is a malformed shape. It must surface a
        # code-owned container_group_definitions/malformed_response warning and
        # keep the envelope partial, not read as a silent ok.
        listed = {"FleetId": "cf-1", "Status": "ACTIVE", "InstanceType": "c6i.large"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": bad_collection}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        warnings = [(w.get("Source"), w.get("Code")) for w in result["Warnings"]]
        assert ("container_group_definitions", "malformed_response") in warnings, warnings
        assert result.get("partial") is True
        assert result["status"] == "incomplete"
        # The group-definition read is enrichment for container-fleet rows: the
        # fault loses enrichment but discards no fleet row, so the envelope is
        # partial with a warning only, never a truncated/errored view (the
        # container row is retained).
        assert result["FleetCounts"]["Container"] == 1
        assert "truncated" not in result, result
        assert "error" not in result, result.get("error")
        _assert_no_sensitive(result)

    @pytest.mark.parametrize("bad_collection", [None, 7, "deps", {"DeploymentId": "d"}])
    def test_malformed_deployment_page_surfaces_warning(self, bad_collection):
        # A present-but-wrong-typed FleetDeployments collection on the per-fleet
        # deployment read is a malformed shape and must surface a code-owned
        # fleet_deployments/malformed_response warning on a partial envelope.
        listed = {
            "FleetId": "cf-1",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "DeploymentDetails": {"LatestDeploymentId": "d-1"},
        }
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            list_fleet_deployments=[{"FleetDeployments": bad_collection}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        warnings = [(w.get("Source"), w.get("Code")) for w in result["Warnings"]]
        assert ("fleet_deployments", "malformed_response") in warnings, warnings
        assert result.get("partial") is True
        assert result["status"] == "incomplete"
        # Same treatment as a group-definition fault: a deployment-read fault
        # loses one field but discards no fleet row, so the envelope is partial
        # with a warning only and carries no truncation marker or error code.
        assert result["FleetCounts"]["Container"] == 1
        assert "truncated" not in result, result
        assert "error" not in result, result.get("error")
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# Group-definition list rows with an unusable Name/VersionNumber are skipped.
# ---------------------------------------------------------------------------
class TestGroupDefinitionListRowSafety:
    def test_unhashable_name_skipped_and_later_definitions_kept(self):
        # A group-definition list row whose Name is unhashable (a list) is not a
        # usable key. It must be skipped with a code-owned malformed_response
        # warning, and a following valid definition must still be indexed so the
        # fleet that uses it keeps its describe-free list-time definition.
        bad_def = {"Name": ["unhashable"], "Status": "READY"}
        good_def = {"Name": "game-server-group", "Status": "READY", "ContainerGroupType": "GAME_SERVER"}
        listed = {
            "FleetId": "cf-1",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "GameServerContainerGroupDefinitionName": "game-server-group",
        }
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            # The bad row precedes the good row; a naive key build raises on the
            # bad row and drops the good row that follows it.
            list_container_group_definitions=[{"ContainerGroupDefinitions": [bad_def, good_def]}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            # No usable describe result -> the row must fall back to the indexed
            # list-time definition, which proves the good row was kept.
            describe_container_group_definition=lambda **kw: {},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        warnings = [(w.get("Source"), w.get("Code")) for w in result["Warnings"]]
        assert ("container_group_definitions", "malformed_response") in warnings, warnings
        # A local shape fault here must not be labeled a provider error.
        assert ("container_group_definitions", "provider_error") not in warnings, warnings
        assert result["FleetCounts"]["Container"] == 1
        row = result["ContainerFleets"][0]
        assert (
            row.get("ContainerGroupDefinition", {}).get("Name") == "game-server-group"
        ), "the valid definition after the skipped bad row was lost"
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# A group-definition read/index fault loses only container-fleet enrichment, so
# a listing that retains every fleet row stays partial with a warning and never
# gains a truncation marker or error code.
# ---------------------------------------------------------------------------
class TestGroupDefinitionFaultRetainsRowsNoTruncation:
    @staticmethod
    def _fake_with_group_defs(group_defs_page: Any) -> "RecordingGameLift":
        # One valid container fleet that projects cleanly on its own. The row
        # carries no GameServerContainerGroupDefinitionName, so it needs no group
        # definition to project, and a fault in the group-definition listing
        # discards no fleet row.
        listed = {"FleetId": "cf-1", "Status": "ACTIVE", "InstanceType": "c6i.large"}
        return RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[group_defs_page],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )

    def _assert_partial_warned_not_truncated(self, result: dict[str, Any]) -> None:
        warnings = [(w.get("Source"), w.get("Code")) for w in result["Warnings"]]
        assert ("container_group_definitions", "malformed_response") in warnings, warnings
        assert result["status"] == "incomplete", result["status"]
        assert result.get("partial") is True
        # Every fleet row is retained: nothing was discarded, so there is no
        # truncated view and no pinned error code.
        assert result["FleetCounts"]["Container"] == 1
        assert "truncated" not in result, result
        assert "error" not in result, result.get("error")
        _assert_no_sensitive(result)

    @pytest.mark.parametrize("bad_collection", [None, 7, "defs", {"Name": "d"}])
    def test_malformed_group_definition_page_keeps_rows(self, bad_collection):
        # A present-but-wrong-typed ContainerGroupDefinitions collection.
        fake = self._fake_with_group_defs({"ContainerGroupDefinitions": bad_collection})
        self._assert_partial_warned_not_truncated(_run(fake))

    def test_group_definition_row_with_list_name_keeps_rows(self):
        # A row whose Name is a list is not a usable key; it is skipped with a
        # warning, discarding no fleet row.
        page = {"ContainerGroupDefinitions": [{"Name": ["not", "hashable"], "Status": "READY"}]}
        self._assert_partial_warned_not_truncated(_run(self._fake_with_group_defs(page)))

    def test_group_definition_row_with_empty_name_keeps_rows(self):
        # An empty Name cannot key a definition, so the row is skipped with a
        # warning and no fleet row is discarded.
        page = {"ContainerGroupDefinitions": [{"Name": "", "Status": "READY"}]}
        self._assert_partial_warned_not_truncated(_run(self._fake_with_group_defs(page)))

    @pytest.mark.parametrize("bad_version", ["3", 1.5, True])
    def test_group_definition_row_with_non_int_version_keeps_rows(self, bad_version):
        # A string, float, or boolean VersionNumber is not an int key; the row
        # is skipped with a warning and no fleet row is discarded.
        page = {"ContainerGroupDefinitions": [{"Name": "unused-def", "VersionNumber": bad_version}]}
        self._assert_partial_warned_not_truncated(_run(self._fake_with_group_defs(page)))

    @pytest.mark.parametrize("bad_row", [None, 7, "not-a-mapping", ["x"]])
    def test_group_definition_non_mapping_row_keeps_rows(self, bad_row):
        # A non-mapping group-definition row carries no usable enrichment. It is
        # skipped with a warning and discards no fleet row.
        page = {"ContainerGroupDefinitions": [bad_row]}
        self._assert_partial_warned_not_truncated(_run(self._fake_with_group_defs(page)))


# ---------------------------------------------------------------------------
# A non-mapping deployment row loses only the latest-deployment enrichment, so a
# listing that retains every fleet row stays partial with a warning and never
# gains a truncation marker or error code — the deployment-side twin of a
# non-mapping group-definition row.
# ---------------------------------------------------------------------------
class TestDeploymentRowFaultRetainsRowsNoTruncation:
    @staticmethod
    def _fake_with_deployment_rows(deployment_rows: Any) -> "RecordingGameLift":
        # One valid container fleet that names its latest deployment, so the
        # per-fleet deployment read runs. The fleet projects cleanly on its own,
        # so a malformed deployment row costs only the DeploymentStatus field and
        # discards no fleet row.
        listed = {
            "FleetId": "cf-1",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "DeploymentDetails": {"LatestDeploymentId": "dep-1"},
        }
        return RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            list_fleet_deployments=[{"FleetDeployments": deployment_rows}],
            list_fleets=[{"FleetIds": []}],
        )

    @pytest.mark.parametrize("bad_row", [None, 7, "not-a-mapping", ["x"]])
    def test_non_mapping_deployment_row_keeps_row_and_warns(self, bad_row):
        result = _run(self._fake_with_deployment_rows([bad_row]))
        warnings = [(w.get("Source"), w.get("Code")) for w in result["Warnings"]]
        assert ("fleet_deployments", "malformed_response") in warnings, warnings
        assert result["status"] == "incomplete", result["status"]
        assert result.get("partial") is True
        # The fleet row is retained; only the DeploymentStatus enrichment is lost.
        assert result["FleetCounts"]["Container"] == 1
        container = result["ContainerFleets"][0]
        assert "DeploymentStatus" not in container, container
        assert "truncated" not in result, result
        assert "error" not in result, result.get("error")
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# Terminal malformed listing pins the typed malformed_response error code.
# ---------------------------------------------------------------------------
class TestTerminalMalformedErrorCode:
    def test_only_container_falls_back_to_unprojectable_list_time_data(self):
        # The only container fleet fails its describe (falling back to list-time
        # data) AND its list-time fields are all invalid, so it projects to
        # nothing. With no rows retained and no typed provider error, the
        # envelope must pin the typed malformed_response code rather than deriving
        # the code from a warning marker such as stale_list_time_data.
        listed = {"FleetId": "cf-1", "Status": "not-a-valid-status", "InstanceType": "not.a.size"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            # Missing ContainerFleet -> list-time fallback (stale), and the
            # list-time fields above do not project to a usable row.
            describe_container_fleet=lambda **kw: {},
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result["ClassicFleets"] == []
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "malformed_response", result.get("error")
        _assert_no_sensitive(result)

    def test_typed_code_and_stale_marker_coexist_pins_typed_code(self):
        # A classic listing is throttled (a typed provider error) while a
        # container fleet goes stale, and neither returns a row. The envelope
        # must pin the typed throttled code; the stale_list_time_data marker must
        # never become the error code.
        throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "m"}}, "ListFleets")
        listed = {"FleetId": "cf-1", "Status": "not-a-valid-status", "InstanceType": "not.a.size"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": []}],
            describe_container_fleet=lambda **kw: {},  # stale fallback, projects to nothing
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=throttled,
        )
        result = _run(fake)
        assert result["ClassicFleets"] == []
        assert result["ContainerFleets"] == []
        assert result["status"] == "incomplete"
        assert result["error"]["code"] == "throttled", result.get("error")
        _assert_no_sensitive(result)


# ---------------------------------------------------------------------------
# Local-fault and non-mapping group-definition response are pinned in the log.
# ---------------------------------------------------------------------------
class TestLocalFaultLogging:
    def test_safety_net_logs_local_fault_message_and_typed_code_without_exception(self):
        # Force a LOCAL (non-provider) fault inside the per-fleet helper and
        # capture the real Loguru sink: the record must carry the code-owned
        # local-fault message and the typed malformed_response code, must NOT be
        # the provider-call-failed message, must NOT attach an exception or
        # traceback, and must not leak the hostile text carried by the fault.
        # Local modules
        import agents.gamelift_specialist as gls
        from utils.logger import logger

        hostile = f"boom {SYNTHETIC_FLEET_ARN} at {SYNTHETIC_URL}"
        records: list[dict[str, Any]] = []

        def sink(message):
            record = message.record
            records.append(
                {
                    "message": record["message"],
                    "extra": dict(record["extra"]),
                    "has_exception": record["exception"] is not None,
                    "text": str(message),
                }
            )

        sink_id = logger.add(sink, level="DEBUG")
        cfg = {
            **_container_base_cfg(),
            "list_container_fleets": [{"ContainerFleets": [_GOOD_CONTAINER]}],
            "describe_container_fleet": lambda **kw: {"ContainerFleet": _GOOD_CONTAINER},
        }
        fake = RecordingGameLift(**cfg)
        try:
            with patch.object(gls, "project_container_fleet_summary", side_effect=RuntimeError(hostile)):
                result = _run(fake)
        finally:
            logger.remove(sink_id)

        codes = {w.get("Code") for w in result["Warnings"]}
        assert "malformed_response" in codes
        assert "provider_error" not in codes
        local_faults = [
            r for r in records if r["message"] == "GameLift response assembly failed on a local shape fault"
        ]
        assert local_faults, "the local-fault message was not logged"
        for record in local_faults:
            assert record["extra"].get("error_code") == "malformed_response"
            assert record["has_exception"] is False, "a local fault must not attach the exception/traceback"
        blob = "\n".join(r["text"] for r in records) + "\n".join(json.dumps(r["extra"], default=str) for r in records)
        assert "boom" not in blob
        assert SYNTHETIC_FLEET_ARN not in blob
        assert "arn:aws" not in blob
        assert SYNTHETIC_URL not in blob
        assert SYNTHETIC_ACCOUNT_ID not in blob

    def test_non_mapping_describe_group_definition_response_keeps_list_time_def(self):
        # describe_container_group_definition returns a NON-MAPPING response. The
        # row must keep its list-time definition, set stale, record a code-owned
        # warning, and never cache the non-mapping value.
        listed = {
            "FleetId": "cf-1",
            "Status": "ACTIVE",
            "InstanceType": "c6i.large",
            "GameServerContainerGroupDefinitionName": "game-server-group",
        }
        list_time_def = {"Name": "game-server-group", "Status": "READY", "ContainerGroupType": "GAME_SERVER"}
        fake = RecordingGameLift(
            list_container_fleets=[{"ContainerFleets": [listed]}],
            list_container_group_definitions=[{"ContainerGroupDefinitions": [list_time_def]}],
            describe_container_fleet=lambda **kw: {"ContainerFleet": listed},
            describe_container_group_definition=lambda **kw: "not-a-mapping",
            list_fleet_deployments=[{"FleetDeployments": []}],
            list_fleets=[{"FleetIds": []}],
        )
        result = _run(fake)
        assert result.get("stale") is True, "a non-mapping describe response must set stale"
        codes = {w.get("Code") for w in result["Warnings"]}
        assert "stale_list_time_data" in codes
        assert result["FleetCounts"]["Container"] == 1
        assert result["ContainerFleets"][0].get("ContainerGroupDefinition", {}).get("Name") == "game-server-group"
        _assert_no_sensitive(result)
