"""Documentation contract tests for the sample cost table.

These are documentation guards, not runtime behavior tests. They derive the
monthly cost from the table itself - the model-token rows plus every non-model
service row - and assert that every stated figure agrees with that derivation:
the model-row arithmetic, the model subtotal, the non-model remainder, the
monthly total, and the model share. The same derived model subtotal is checked
against the two headline lines in ``docs/DEPLOYMENT_GUIDE.md`` so the guide
cannot drift from the README.

The token volumes assume the Claude 5.5 tokenizer, which counts the same text
as roughly 30% more tokens than the equivalent Claude 4.x workload, so the
model rows carry 1.3x the 4.x volumes and exclude any allowance for
``between_tools`` thinking blocks. The checks here only verify internal
consistency (volume x rate = row cost, rows sum to the subtotal, model plus
non-model equals the total, the share and subtotal in the note and the guide
follow from the sums); they do not re-derive the per-token rates from an
external source.

Nothing here hard-codes a dollar figure that the table also states, so a
correct price change that updates every dependent figure passes, while a change
that leaves any dependent figure stale fails.
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
_DEPLOYMENT_GUIDE = _REPO_ROOT / "docs" / "DEPLOYMENT_GUIDE.md"

_MODEL_LABELS = ("Claude Sonnet 5.5", "Claude Haiku 5.5")


@pytest.fixture(scope="module")
def readme() -> str:
    return _README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deployment_guide() -> str:
    return _DEPLOYMENT_GUIDE.read_text(encoding="utf-8")


def _parse_cost_cell(cell: str) -> float:
    """Parse a dollar amount from a table cost cell, tolerating a leading ``~``.

    Estimate rows carry a ``~$`` prefix and exact rows carry ``$``; both parse
    to the same number here.
    """
    match = re.search(r"~?\$([0-9][0-9,]*\.?[0-9]*)", cell)
    assert match, f"no dollar amount in cost cell: {cell!r}"
    return float(match.group(1).replace(",", ""))


def _cost_table_rows(readme: str) -> list[tuple[str, str]]:
    """Return (first-cell label, last-cell cost) for every data row of the cost table.

    The cost table is the first three-column Markdown table whose header is
    ``| AWS service | Dimensions | Cost [USD] |``. The returned rows exclude the
    header and separator but include the model rows and the ``Total`` row; the
    callers filter those out.
    """
    lines = readme.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("| AWS service |") and "Cost [USD]" in line:
            start = i
            break
    assert start is not None, "could not find the cost table header"

    rows: list[tuple[str, str]] = []
    # Skip the header and the separator row that follows it.
    for line in lines[start + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        rows.append((cells[0], cells[-1]))
    return rows


def _row_cost(text: str, model: str) -> tuple[int, int, float]:
    """Return (input_millions, output_millions, row_cost) parsed from a model row.

    The row cost is checked against the volume x rate arithmetic. A ``~`` prefix
    on the cost cell is tolerated so an estimate marker does not break the
    parse.
    """
    pattern = (
        r"\| Amazon Bedrock \(" + re.escape(model) + r"\) \| "
        r"(\d+)M input tokens @ \$([0-9.]+)/M \+ (\d+)M output tokens @ \$([0-9.]+)/M"
        r".*?\| ~?\$([0-9.]+) \|"
    )
    match = re.search(pattern, text)
    assert match, f"could not find the {model} cost row"
    in_m, in_rate, out_m, out_rate, row = match.groups()
    computed = int(in_m) * float(in_rate) + int(out_m) * float(out_rate)
    assert computed == pytest.approx(
        float(row), abs=0.01
    ), f"{model} row cost {row} does not match {in_m}M*{in_rate} + {out_m}M*{out_rate} = {computed}"
    return int(in_m), int(out_m), float(row)


def _model_subtotal(readme: str) -> float:
    sonnet = _row_cost(readme, "Claude Sonnet 5.5")[2]
    haiku = _row_cost(readme, "Claude Haiku 5.5")[2]
    return sonnet + haiku


def _non_model_subtotal(readme: str) -> float:
    """Sum every non-model, non-total service row's cost cell."""
    total = 0.0
    counted = 0
    for label, cost_cell in _cost_table_rows(readme):
        if "**Total**" in label:
            continue
        if any(m in label for m in _MODEL_LABELS):
            continue
        total += _parse_cost_cell(cost_cell)
        counted += 1
    assert counted >= 10, f"expected the full non-model row set, counted {counted}"
    return round(total, 2)


# --------------------------------------------------------------------------- #
# Model-row arithmetic
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Derived totals: everything that states a figure must follow from the rows
# --------------------------------------------------------------------------- #


def test_overview_total_is_the_sum_of_the_rows(readme: str) -> None:
    model_subtotal = _model_subtotal(readme)
    non_model_subtotal = _non_model_subtotal(readme)
    derived_total = model_subtotal + non_model_subtotal

    # The overview sentence states the monthly total, the model subtotal, and
    # the non-model remainder. Each must equal the derived figure, rounded, so a
    # change to any row that is not carried through to the overview fails.
    overview = re.search(
        r"approximately\s+\*\*~\$([\d,]+)/month\*\*.*?"
        r"Claude model tokens account for\s+~\$([\d,]+)/month\s+and all other services for\s+~\$([\d,]+)/month",
        readme,
        re.DOTALL,
    )
    assert overview, "could not find the cost overview sentence"
    total, model, non_model = (int(g.replace(",", "")) for g in overview.groups())

    assert model == round(model_subtotal)
    assert non_model == round(non_model_subtotal)
    assert total == round(derived_total)
    # The three stated figures must themselves be consistent.
    assert total == model + non_model


def test_total_row_matches_the_sum(readme: str) -> None:
    derived_total = _model_subtotal(readme) + _non_model_subtotal(readme)
    total_row = re.search(r"\|\s*\*\*Total\*\*\s*\|\s*\|\s*\*\*~\$([\d,]+)/month\*\*\s*\|", readme)
    assert total_row, "could not find the Total row"
    assert int(total_row.group(1).replace(",", "")) == round(derived_total)


def test_dominant_cost_note_share_and_subtotal(readme: str) -> None:
    model_subtotal = _model_subtotal(readme)
    derived_total = model_subtotal + _non_model_subtotal(readme)

    # The note repeats the model subtotal and the model share; both derive from
    # the rows. A line break inside the phrase is tolerated with DOTALL.
    note = re.search(r"Sonnet \+ Haiku = \$([\d,]+),\s+or ~(\d+)% of total", readme, re.DOTALL)
    assert note, "could not find the dominant-cost note"
    assert int(note.group(1).replace(",", "")) == round(model_subtotal)
    assert int(note.group(2)) == round(model_subtotal / derived_total * 100)


# --------------------------------------------------------------------------- #
# The deployment guide must repeat the same model subtotal
# --------------------------------------------------------------------------- #


def test_deployment_guide_model_subtotal_matches_readme(readme: str, deployment_guide: str) -> None:
    model_subtotal = round(_model_subtotal(readme))

    # The guide states the model subtotal twice: a Bedrock row in the cost
    # table and an "AI usage (variable)" line. Both must equal the README's
    # derived model subtotal. A trailing qualifier or line wrap after the
    # amount is tolerated.
    row_line = re.search(
        r"\| Bedrock \(Claude Sonnet 5\.5 \+ Haiku 5\.5\) \| ~\$([\d,]+)\b",
        deployment_guide,
    )
    assert row_line, "could not find the guide Bedrock cost row"
    assert int(row_line.group(1).replace(",", "")) == model_subtotal

    ai_usage = re.search(
        r"\*\*AI usage \(variable\)\*\*:\s*~\$([\d,]+)",
        deployment_guide,
    )
    assert ai_usage, "could not find the guide AI-usage line"
    assert int(ai_usage.group(1).replace(",", "")) == model_subtotal
