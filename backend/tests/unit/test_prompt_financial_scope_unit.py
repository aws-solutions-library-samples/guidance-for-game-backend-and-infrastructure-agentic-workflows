"""Non-cost prompts keep money out of operational answers.

Only the validated Cost Explorer report path may emit financial figures. When a
GameLift or EKS answer volunteers cost, pricing, or savings commentary, the
orchestrator's fail-closed financial guard replaces the whole answer with a
cost notice, so the user loses the operational content they asked for. The
specialist and orchestrator prompts therefore tell the models to leave money to
the cost specialist.
"""

# Third-party packages
import pytest

# Local modules
from agents.financial_guard import contains_unvalidated_financial_content
from agents.optimized_prompts import EKS_PROMPT, GAMELIFT_PROMPT, ORCHESTRATOR_PROMPT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prompt", [GAMELIFT_PROMPT, EKS_PROMPT], ids=lambda p: p.name)
def test_operational_specialists_leave_money_to_the_cost_specialist(prompt):
    text = prompt.text
    assert "Do not discuss pricing, costs, savings, or billing" in text
    assert "never state a monetary amount" in text
    assert "the cost specialist answers money questions" in text


def test_orchestrator_adds_no_cost_commentary_to_operational_answers():
    assert "Do not add cost, pricing, or savings commentary to a GameLift or EKS answer" in ORCHESTRATOR_PROMPT.text


@pytest.mark.parametrize(
    ("prompt", "version"),
    [(GAMELIFT_PROMPT, "2.3.0"), (EKS_PROMPT, "2.2.0"), (ORCHESTRATOR_PROMPT, "2.2.0")],
    ids=lambda value: getattr(value, "name", value),
)
def test_prompt_versions_record_the_directive(prompt, version):
    assert prompt.version == version


@pytest.mark.parametrize("prompt", [GAMELIFT_PROMPT, EKS_PROMPT, ORCHESTRATOR_PROMPT], ids=lambda p: p.name)
def test_prompt_text_itself_is_financially_clean(prompt):
    """The directive must not itself look like a financial figure to the guard."""
    assert not contains_unvalidated_financial_content(prompt.text)
