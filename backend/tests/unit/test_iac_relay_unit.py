"""Unit tests for deterministic relay of specialist-generated IaC."""

# Standard library
from unittest.mock import patch

# Third-party packages
import pytest

# Local modules
import agents.orchestrator as orch
from agents.iac_relay import (
    MAX_BLOCK_CHARS,
    MAX_RELAYED_BLOCKS,
    RELAY_HEADING,
    extract_iac_blocks,
    relay_specialist_iac,
    strip_iac_blocks,
)
from agents.specialist_capture import record_specialist_output

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
    huge = "```yaml\nResources:\n" + ("  # pad\n" * (MAX_BLOCK_CHARS // 8 + 10)) + "```"
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
    assert "read-only and has not created any resources" in result


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
    assert "The answer below was generated by the GameLift specialist" in result


def test_financial_prose_falls_back_to_template_only():
    priced_plan = SPECIALIST_OUTPUT.replace("## Migration plan", "## Migration plan\n\nThis saves $400 per month.")
    result = relay_specialist_iac("Overview.", [("GameLift", priced_plan)])
    assert "$400" not in result
    assert "## Migration plan" not in result
    assert TEMPLATE in result
    assert "The template below was generated by the GameLift specialist" in result


@pytest.fixture
def _fake_orchestrator_agent():
    """Run the real run_orchestrator with a fake routing model."""

    def make(final_text: str, specialist: tuple[str, str] | None):
        def fake_agent(*_args, **_kwargs):
            def call(_query):
                if specialist:
                    record_specialist_output(*specialist)
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
