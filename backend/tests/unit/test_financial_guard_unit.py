"""Unit tests for the fail-closed financial content guard."""

# Third-party packages
import pytest

# Local modules
from agents.financial_guard import (
    contains_unvalidated_financial_content,
    sanitize_advisory_section,
    sanitize_specialist_section,
)

pytestmark = pytest.mark.unit


class TestContainsUnvalidatedFinancialContent:
    @pytest.mark.parametrize(
        "text",
        [
            "The total is USD 999.00",
            "USD999",
            "999.00 USD",
            "999USD",
            "It costs $999",
            "$1,234.56 for the month",
            "12 €",
            "€ 12",
            "£50 per node",
            "That is 250 dollars",
            "roughly 5 cents each",
            "The bill total is 999.00 this cycle",
            "spending reached 1,234.50 last week",
            "Monthly cost is 999",
            "Monthly cost is 5.5",
            "Monthly cost is .5",
            "Monthly cost is 888.",
            "Monthly cost:\n\n999",
            "The budget is 200",
            "Expected savings are 3.4",
            "Expected savings are 25%",
            "Save 25% with Spot capacity.",
            "Your bill could drop by 25%.",
            "The estimate is AED 999.00 per month.",
            "On-demand rate: 0.20 per instance-hour.",
            "Expected savings:\n\n25%",
            "Monthly cost:\n999",
            "Monthly cost is `999`",
            "`Monthly cost is $5`",
            "```bash\necho 'Monthly cost is $5'\n```",
            "```bash\nprintf '%s\\n' '$5'\n```",
            '```json\n{"cost": 999}\n```',
            '```json\n{"cost": "$5"}\n```',
        ],
    )
    def test_flags_financial_content(self, text):
        assert contains_unvalidated_financial_content(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "## EKS Clusters\n\n- game-cluster: ACTIVE (3 nodes)",
            "Fleet fleet-alpha has 5 instances across 2 regions",
            "Open port 7777 for UDP game traffic",
            "Scale the deployment to 10 replicas",
            "Kubernetes version 1.30 is supported",
            "CPU utilization is 42% and memory is 1.50 GiB",
            "Reduce cost by right-sizing (see recommendations)",
            "A 30.00% utilization improvement is possible",
            "Total latency is 12.00 ms",
            "Time was spent restarting pods",
            "Dollars and yen are currency names",
            "Run this shell snippet: `printf %s $1`",
            "```bash\nprintf '%s' \"$1\"\n```",
            "awk '{print $1}' input.txt",
        ],
    )
    def test_preserves_nonfinancial_operational_text(self, text):
        assert contains_unvalidated_financial_content(text) is False


class TestSanitizeSpecialistSection:
    def test_clean_section_passes_through_verbatim(self):
        clean = "## GameLift Fleets\n\n- fleet-alpha: ACTIVE"
        assert sanitize_specialist_section("GameLift", clean) == clean

    def test_financial_section_replaced_with_number_free_notice(self):
        malicious = "## EKS\n\nModel-authored EKS cost: USD 999.00"
        result = sanitize_specialist_section("EKS", malicious)

        assert "USD 999.00" not in result
        assert "999" not in result
        assert result.startswith("## EKS")
        assert "withheld" in result.lower()
        # The notice itself must remain financially clean (no numbers/currency).
        assert contains_unvalidated_financial_content(result) is False

    def test_notice_uses_service_name(self):
        result = sanitize_specialist_section("GameLift", "price is $5")
        assert "GameLift" in result
        assert "$5" not in result


class TestSanitizeAdvisorySection:
    def test_removes_cross_paragraph_financial_claim_and_keeps_advice(self):
        text = "Use Spot capacity and right-size instances.\n\nMonthly cost:\n\n999"

        result = sanitize_advisory_section("Cost", text)

        assert "Use Spot capacity and right-size instances." not in result
        assert "Monthly cost" not in result
        assert "999" not in result
        assert "right-sizing" in result
        assert "financial figures were withheld" in result

    def test_removes_cross_paragraph_financial_percentage(self):
        text = "Review idle resources first.\n\nExpected savings:\n\n25%"

        result = sanitize_advisory_section("Cost", text)

        assert "Review idle resources first." not in result
        assert "Expected savings" not in result
        assert "25%" not in result
        assert "idle resources" in result


def test_advisory_sanitizer_links_financial_label_across_one_intermediary_paragraph():
    text = "Use Spot capacity first.\n\nMonthly cost:\n\nEstimate follows.\n\n999"

    result = sanitize_advisory_section("Cost", text)

    assert "Use Spot capacity first." not in result
    assert "Monthly cost" not in result
    assert "Estimate follows" not in result
    assert "999" not in result
    assert "commitment options" in result
