"""Documentation contract tests for the README sample cost table.

These are documentation guards, not runtime behavior tests. They pin the
arithmetic of the model-token rows so the per-token rates, the token volumes,
the row costs, the model subtotal, and the monthly total cannot drift out of
agreement with one another.

The token volumes assume the Claude 5.5 tokenizer, which counts the same text
as roughly 30% more tokens than the equivalent Claude 4.x workload, so the
rows carry 1.3x the 4.x volumes. The checks here only verify internal
consistency (volume x rate = row cost, rows sum to the subtotal, subtotal plus
the stated non-model remainder equals the total); they do not re-derive the
rates from an external source.
"""

from __future__ import annotations

# Standard library
import re
from pathlib import Path

# Third-party packages
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_README = _REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return _README.read_text(encoding="utf-8")


def _row_cost(text: str, model: str) -> tuple[float, float, float]:
    """Return (input_millions, output_millions, row_cost) parsed from a model row."""
    pattern = (
        r"\| Amazon Bedrock \(" + re.escape(model) + r"\) \| "
        r"(\d+)M input tokens @ \$([0-9.]+)/M \+ (\d+)M output tokens @ \$([0-9.]+)/M"
        r".*?\| \$([0-9.]+) \|"
    )
    match = re.search(pattern, text)
    assert match, f"could not find the {model} cost row"
    in_m, in_rate, out_m, out_rate, row = match.groups()
    computed = int(in_m) * float(in_rate) + int(out_m) * float(out_rate)
    assert computed == pytest.approx(
        float(row), abs=0.01
    ), f"{model} row cost {row} does not match {in_m}M*{in_rate} + {out_m}M*{out_rate} = {computed}"
    return int(in_m), int(out_m), float(row)


def test_sonnet_row_arithmetic(readme: str) -> None:
    in_m, out_m, row = _row_cost(readme, "Claude Sonnet 5.5")
    # 1.3x the 80M/20M Claude 4.x workload.
    assert (in_m, out_m) == (104, 26)
    assert row == pytest.approx(468.00, abs=0.01)


def test_haiku_row_arithmetic(readme: str) -> None:
    in_m, out_m, row = _row_cost(readme, "Claude Haiku 5.5")
    # 1.3x the 40M/10M Claude 4.x workload.
    assert (in_m, out_m) == (52, 13)
    assert row == pytest.approx(11.70, abs=0.01)


def test_model_subtotal_and_total_are_consistent(readme: str) -> None:
    _, _, sonnet = _row_cost(readme, "Claude Sonnet 5.5")
    _, _, haiku = _row_cost(readme, "Claude Haiku 5.5")
    model_subtotal = sonnet + haiku
    assert model_subtotal == pytest.approx(479.70, abs=0.01)

    # The overview sentence states the model subtotal and the non-model
    # remainder; both must agree with the rounded subtotal and the total.
    overview = re.search(
        r"approximately \*\*~\$(\d+)/month\*\*.*?Claude model tokens account for "
        r"~\$(\d+)/month and all other services for ~\$(\d+)/month",
        readme,
        re.DOTALL,
    )
    assert overview, "could not find the cost overview sentence"
    total, model, nonmodel = (int(g) for g in overview.groups())
    assert model == round(model_subtotal)  # ~$480
    assert total == model + nonmodel  # $480 + $122 = $602
    assert total == 602

    # The table Total row must repeat the same monthly total.
    total_row = re.search(r"\| \*\*Total\*\* \| \| \*\*~\$(\d+)/month\*\* \|", readme)
    assert total_row, "could not find the Total row"
    assert int(total_row.group(1)) == total

    # The dominant-cost note must repeat the model subtotal.
    note = re.search(r"Sonnet \+ Haiku = \$(\d+), or ~(\d+)% of total", readme)
    assert note, "could not find the dominant-cost note"
    assert int(note.group(1)) == round(model_subtotal)
    assert int(note.group(2)) == round(model_subtotal / total * 100)
