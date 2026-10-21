"""Unit tests for deterministic relay of specialist-generated IaC."""

# Standard library
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
import agents.orchestrator as orch
from agents.iac_relay import (
    MAX_BLOCK_BYTES,
    MAX_RELAYED_BLOCKS,
    RELAY_HEADING,
    extract_iac_blocks,
    relay_specialist_iac,
    strip_iac_blocks,
)
from agents.specialist_capture import begin_specialist_call, record_specialist_output

pytestmark = pytest.mark.unit

# conftest patches run_orchestrator for unit tests; keep the real one.
_real_run_orchestrator = orch.run_orchestrator

TEMPLATE = """```yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: Agones simple-game-server migrated to a GameLift container fleet
Parameters:
  GameServerImageUri:
    Type: String
Resources:
  FleetRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal: { Service: gamelift.amazonaws.com }
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - !Sub arn:${AWS::Partition}:iam::aws:policy/GameLiftContainerFleetPolicy
  GameServerGroup:
    Type: AWS::GameLift::ContainerGroupDefinition
    Properties:
      Name: !Sub ${AWS::StackName}-game-server
      OperatingSystem: AMAZON_LINUX_2023
      TotalMemoryLimitMebibytes: 1024
      TotalVcpuLimit: 0.5
      GameServerContainerDefinition:
        ContainerName: game-server
        ImageUri: !Ref GameServerImageUri
        ServerSdkVersion: "5.2.0"
        PortConfiguration:
          ContainerPortRanges:
            - { FromPort: 7654, ToPort: 7654, Protocol: UDP }
  Fleet:
    Type: AWS::GameLift::ContainerFleet
    Properties:
      FleetRoleArn: !GetAtt FleetRole.Arn
      GameServerContainerGroupDefinitionName: !GetAtt GameServerGroup.ContainerGroupDefinitionArn
      InstanceType: c6i.large
      ScalingPolicies:
        - Name: keep-20-percent-available
          PolicyType: TargetBased
          MetricName: PercentAvailableGameSessions
          TargetConfiguration: { TargetValue: 20 }
  Queue:
    Type: AWS::GameLift::GameSessionQueue
    Properties:
      Name: !Sub ${AWS::StackName}-queue
      Destinations:
        - DestinationArn: !GetAtt Fleet.FleetArn
```"""

SPECIALIST_OUTPUT = (
    "## Migration plan\n\nThe Agones fleet maps to a GameLift container fleet.\n\n"
    f"{TEMPLATE}\n\n- Deploy with aws cloudformation deploy.\n"
)


def test_extracts_complete_cloudformation_block():
    blocks = extract_iac_blocks(SPECIALIST_OUTPUT)
    assert blocks == [TEMPLATE]


def test_ignores_non_iac_yaml_and_other_languages():
    text = (
        "```yaml\nmetadata:\n  labels:\n    app: game\n```\n\n"
        "```bash\naws cloudformation deploy --template-file t.yaml\n```\n"
    )
    assert extract_iac_blocks(text) == []


def test_unterminated_block_is_never_relayed():
    truncated = SPECIALIST_OUTPUT.split("  Queue:")[0]  # cut mid-template, no closing fence
    assert extract_iac_blocks(truncated) == []


def test_filename_tokens_and_terraform_are_recognized():
    text = '```main.tf\nresource "aws_gamelift_fleet" "x" {}\n```\n\n```template.yaml\nResources:\n  A:\n    Type: AWS::S3::Bucket\n```'
    assert len(extract_iac_blocks(text)) == 2


def test_bounds_block_count_and_size():
    many = "\n\n".join([TEMPLATE] * (MAX_RELAYED_BLOCKS + 2))
    assert len(extract_iac_blocks(many)) == MAX_RELAYED_BLOCKS
    huge = "```yaml\nResources:\n" + ("  # pad\n" * (MAX_BLOCK_BYTES // 8 + 10)) + "```"
    assert extract_iac_blocks(huge) == []


def test_strip_removes_only_iac_blocks():
    prose = f"Summary.\n\n{TEMPLATE}\n\n```bash\nkubectl get fleets\n```"
    stripped = strip_iac_blocks(prose)
    assert "AWSTemplateFormatVersion" not in stripped
    assert "kubectl get fleets" in stripped
    assert stripped.startswith("Summary.")


def test_relay_appends_specialist_template_verbatim_and_replaces_retyped_copy():
    retyped = "Here is the plan.\n\n```yaml\nAWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n```"
    result = relay_specialist_iac(retyped, [("GameLift", SPECIALIST_OUTPUT)])
    assert result.startswith("Here is the plan.")
    assert RELAY_HEADING in result
    assert TEMPLATE in result
    assert "Resources: {}" not in result
    assert "has not created, changed, or deployed any resources" in result


def test_relay_is_noop_without_specialist_iac():
    answer = "You have 2 fleets."
    assert relay_specialist_iac(answer, [("GameLift", "No template here.")]) == answer
    assert relay_specialist_iac(answer, []) == answer


def test_relay_ignores_cost_specialist_output():
    answer = "Done."
    assert relay_specialist_iac(answer, [("Cost", SPECIALIST_OUTPUT)]) == answer


def test_cloudformation_substitutions_do_not_trip_financial_guard():
    # Local modules
    from agents.financial_guard import contains_unvalidated_financial_content

    assert not contains_unvalidated_financial_content(TEMPLATE)


def test_block_with_financial_content_is_withheld_visibly():
    priced = TEMPLATE.replace("InstanceType: c6i.large", "InstanceType: c6i.large  # costs $85.00 per month")
    result = relay_specialist_iac("Plan.", [("GameLift", priced)])
    assert "$85.00" not in result
    assert "AWSTemplateFormatVersion" not in result
    assert result.startswith("Plan.")
    assert "was withheld because it contained a monetary value" in result


def test_anywhere_cost_literal_is_withheld():
    anywhere = TEMPLATE.replace(
        "  Queue:",
        "  AnywhereFleet:\n    Type: AWS::GameLift::Fleet\n    Properties:\n"
        '      ComputeType: ANYWHERE\n      AnywhereConfiguration:\n        Cost: "0.1"\n  Queue:',
    )
    result = relay_specialist_iac("Plan.", [("GameLift", anywhere)])
    assert 'Cost: "0.1"' not in result
    assert "was withheld" in result


def test_anywhere_cost_parameter_is_relayed():
    anywhere = TEMPLATE.replace(
        "  Queue:",
        "  AnywhereFleet:\n    Type: AWS::GameLift::Fleet\n    Properties:\n"
        "      ComputeType: ANYWHERE\n      AnywhereConfiguration:\n        Cost: !Ref AnywhereHourlyCost\n  Queue:",
    )
    result = relay_specialist_iac("Plan.", [("GameLift", anywhere)])
    assert "Cost: !Ref AnywhereHourlyCost" in result


def test_clean_specialist_answer_is_relayed_in_full():
    result = relay_specialist_iac("Overview.", [("GameLift", SPECIALIST_OUTPUT)])
    assert "## Migration plan" in result
    assert "Deploy with aws cloudformation deploy." in result
    assert "The answer below is an untrusted draft generated by the GameLift specialist" in result


def test_financial_prose_falls_back_to_template_only():
    priced_plan = SPECIALIST_OUTPUT.replace("## Migration plan", "## Migration plan\n\nThis saves $400 per month.")
    result = relay_specialist_iac("Overview.", [("GameLift", priced_plan)])
    assert "$400" not in result
    assert "## Migration plan" not in result
    assert TEMPLATE in result
    assert "The template below is an untrusted draft generated by the GameLift specialist" in result


FOLLOWUP_TEMPLATE = TEMPLATE.replace("FromPort: 7654, ToPort: 7654", "FromPort: 7777, ToPort: 7786")
FOLLOWUP_OUTPUT = f"## Cutover plan\n\nUse an Anywhere fleet during cutover.\n\n{FOLLOWUP_TEMPLATE}\n"


def test_duplicate_specialist_calls_relay_only_the_first_template():
    result = relay_specialist_iac("Overview.", [("GameLift", SPECIALIST_OUTPUT), ("GameLift", FOLLOWUP_OUTPUT)])
    assert TEMPLATE in result
    assert "7777" not in result
    assert result.count(RELAY_HEADING) == 1
    assert result.count("generated by the GameLift specialist") == 1


def test_later_template_is_used_when_first_call_had_none():
    result = relay_specialist_iac("Overview.", [("GameLift", "No template yet."), ("GameLift", FOLLOWUP_OUTPUT)])
    assert FOLLOWUP_TEMPLATE in result


def test_withheld_first_template_falls_through_to_clean_later_one():
    priced = SPECIALIST_OUTPUT.replace("InstanceType: c6i.large", "InstanceType: c6i.large  # $85.00 per month")
    result = relay_specialist_iac("Overview.", [("GameLift", priced), ("GameLift", FOLLOWUP_OUTPUT)])
    assert "$85.00" not in result
    assert FOLLOWUP_TEMPLATE in result
    assert "was withheld" not in result


def test_withheld_notice_is_emitted_once_per_service():
    priced = SPECIALIST_OUTPUT.replace("InstanceType: c6i.large", "InstanceType: c6i.large  # $85.00 per month")
    result = relay_specialist_iac("Overview.", [("GameLift", priced), ("GameLift", priced)])
    assert result.count("was withheld") == 1


def test_each_service_relays_its_own_first_template():
    eks_output = f"EKS view.\n\n{FOLLOWUP_TEMPLATE}\n"
    result = relay_specialist_iac(
        "Overview.",
        [("GameLift", SPECIALIST_OUTPUT), ("EKS", eks_output), ("GameLift", FOLLOWUP_OUTPUT)],
    )
    assert TEMPLATE in result
    assert result.count(FOLLOWUP_TEMPLATE) == 1
    assert "generated by the EKS specialist" in result


@pytest.fixture
def _fake_orchestrator_agent():
    """Run the real run_orchestrator with a fake routing model."""

    def make(final_text: str, specialist: tuple[str, str] | list[tuple[str, str]] | None):
        calls = specialist if isinstance(specialist, list) else ([specialist] if specialist else [])

        def fake_agent(*_args, **_kwargs):
            def call(_query):
                for service, output in calls:
                    record_specialist_output(service, output, begin_specialist_call(service))
                return final_text

            return call

        return patch.multiple(
            orch,
            Agent=fake_agent,
            USE_BEDROCK_SESSIONS=False,
            create_bedrock_model_with_overrides=lambda **_k: None,
            create_cached_bedrock_model=lambda: None,
        )

    return make


def test_orchestrator_relays_gamelift_template_on_operational_path(_fake_orchestrator_agent):
    with _fake_orchestrator_agent("Migration summary.", ("GameLift", SPECIALIST_OUTPUT)):
        result = _real_run_orchestrator("Migrate my Agones fleet on EKS to GameLift container fleets")
    assert result.startswith("Migration summary.")
    assert TEMPLATE in result


def test_orchestrator_withholds_financial_prose_but_still_relays_template(_fake_orchestrator_agent):
    prose = "Migration summary. This will save you $400 per month."
    with _fake_orchestrator_agent(prose, ("GameLift", SPECIALIST_OUTPUT)):
        result = _real_run_orchestrator("Migrate my Agones fleet on EKS to GameLift container fleets")
    assert "$400" not in result
    assert "Unvalidated financial figures were withheld" in result
    assert TEMPLATE in result


def test_operational_latency_prose_is_not_withheld(_fake_orchestrator_agent):
    prose = "Placement policies balance quality and availability (200 ms, then 500 ms)."
    with _fake_orchestrator_agent(prose, ("GameLift", SPECIALIST_OUTPUT)):
        result = _real_run_orchestrator("Migrate my Agones fleet on EKS to GameLift container fleets")
    assert result.startswith(prose)
    assert TEMPLATE in result


def test_orchestrator_does_not_relay_on_cost_topic(_fake_orchestrator_agent):
    with _fake_orchestrator_agent("Cost summary.", ("GameLift", SPECIALIST_OUTPUT)):
        result = _real_run_orchestrator("How much will this GameLift migration cost me per month?")
    assert TEMPLATE not in str(result)


def test_orchestrator_relays_first_template_when_specialist_called_twice(_fake_orchestrator_agent):
    calls = [("GameLift", SPECIALIST_OUTPUT), ("GameLift", FOLLOWUP_OUTPUT)]
    with _fake_orchestrator_agent("Migration summary.", calls):
        result = _real_run_orchestrator("Migrate my Agones fleet on EKS to GameLift container fleets")
    assert TEMPLATE in result
    assert "7777" not in result


# ---------------------------------------------------------------------------
# Final emitted payload bounds (bytes)
# ---------------------------------------------------------------------------

# Local modules
from agents.iac_relay import (  # noqa: E402
    MAX_PROSE_BYTES,
    MAX_RELAYED_ANSWER_BYTES,
    MAX_RELAYED_TOTAL_BYTES,
    TRUNCATED_NOTICE,
)


def _sized_template(target_bytes: int, marker: str) -> str:
    padding = "  # " + "é" * 20 + "\n"  # multi-byte characters: limits are bytes, not characters
    lines = [f"```yaml\nResources:\n  {marker}:\n    Type: AWS::SNS::Topic\n"]
    while sum(len(line.encode()) for line in lines) < target_bytes:
        lines.append(padding)
    lines.append("```")
    return "".join(lines)


def test_final_payload_never_exceeds_the_total_byte_bound():
    # Two services, each with a ~75 KB answer (prose plus a ~50 KB template):
    # each answer is within its own bound, but together they exceed the total.
    plan = "Step. " * 4_000
    outputs = [
        ("GameLift", f"{plan}\n\n{_sized_template(50_000, 'G')}\n"),
        ("EKS", f"{plan}\n\n{_sized_template(50_000, 'E')}\n"),
    ]
    result = relay_specialist_iac("Overview.", outputs)
    relayed = result.split(RELAY_HEADING, 1)[1]
    assert len((RELAY_HEADING + relayed).encode()) <= MAX_RELAYED_TOTAL_BYTES
    assert TRUNCATED_NOTICE in result
    # Fences are never cut: every emitted opening fence is closed.
    assert relayed.count("```yaml") * 2 == relayed.count("```")


def test_answer_over_byte_bound_falls_back_to_template_only():
    block = _sized_template(10_000, "T")
    long_prose = "é" * (MAX_RELAYED_ANSWER_BYTES // 2 + 10)  # under the char count, over the byte bound
    result = relay_specialist_iac("Overview.", [("GameLift", f"{long_prose}\n\n{block}\n")])
    assert block in result
    assert long_prose not in result
    assert "The template below is an untrusted draft" in result


def test_block_over_byte_bound_is_never_relayed():
    oversized = _sized_template(60_000, "Huge")
    assert extract_iac_blocks(oversized) == []


def test_routing_prose_is_bounded_and_cannot_swallow_the_relayed_section():
    prose = "Overview.\n\n```bash\n" + ("echo filler\n" * 2_000)  # unterminated fence, over the bound
    result = relay_specialist_iac(prose, [("GameLift", SPECIALIST_OUTPUT)])
    head = result.split(RELAY_HEADING, 1)[0]
    assert len(head.encode()) <= MAX_PROSE_BYTES + 16
    assert head.count("```") % 2 == 0
    assert TEMPLATE in result


def test_routing_model_iac_without_a_specialist_source_is_removed():
    # Local modules
    from agents.iac_relay import UNSOURCED_IAC_NOTICE

    answer = f"Here is a migration plan.\n\n{TEMPLATE}\n\n```bash\naws cloudformation deploy\n```"
    result = relay_specialist_iac(answer, [("GameLift", "The GameLift answer was too long to complete.")])
    assert "AWSTemplateFormatVersion" not in result
    assert result.startswith("Here is a migration plan.")
    assert "aws cloudformation deploy" in result  # non-IaC code is kept
    assert result.endswith(UNSOURCED_IAC_NOTICE)


def test_answer_without_any_iac_is_unchanged():
    answer = "You have 2 container fleets.\n\n```bash\naws gamelift list-container-fleets\n```"
    assert relay_specialist_iac(answer, []) == answer


def test_guardrail_masked_values_are_called_out_next_to_the_template():
    masked = TEMPLATE.replace(
        "  Queue:", "      InstanceInboundPermissions:\n        - IpRange: '{IP_ADDRESS}'\n  Queue:"
    )
    result = relay_specialist_iac("Overview.", [("GameLift", f"Plan.\n\n{masked}\n")])
    assert "{IP_ADDRESS}" in result  # shown as produced, not silently rewritten
    assert "masked 1 value(s)" in result
    assert "will not deploy until you replace them" in result


def test_cloudformation_sub_variables_are_not_mistaken_for_masked_values():
    template = TEMPLATE.replace(
        "${AWS::StackName}-queue", "${AWS::StackName}-{NAME}-queue".replace("{NAME}", "${Name}")
    )
    result = relay_specialist_iac("Overview.", [("GameLift", f"Plan.\n\n{template}\n")])
    assert "masked" not in result
