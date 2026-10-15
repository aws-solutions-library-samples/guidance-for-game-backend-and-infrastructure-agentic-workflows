"""Pin the prompt contract for untrusted infrastructure-as-code drafts."""

# Third-party packages
import pytest

# Local modules
from agents.optimized_prompts import GAMELIFT_PROMPT, ORCHESTRATOR_PROMPT

pytestmark = pytest.mark.unit


def test_gamelift_prompt_requires_validation_and_truthful_reporting():
    text = GAMELIFT_PROMPT.text
    assert "validate_cloudformation_template" in text
    assert "untrusted draft" in text
    assert "notValidated" in text
    assert "status incomplete" in text
    assert "Never call a draft deployable, approved, or safe" in text
    assert "never claim that you created, changed, or deployed anything" in text


def test_orchestrator_prompt_does_not_retype_or_overstate_drafts():
    text = ORCHESTRATOR_PROMPT.text
    assert "do not repeat the template" in text
    assert "untrusted draft that has not been applied" in text
    assert "do not claim validation beyond what the specialist reported" in text
