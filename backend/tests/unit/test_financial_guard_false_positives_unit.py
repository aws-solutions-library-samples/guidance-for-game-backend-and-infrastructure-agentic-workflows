"""Unit tests for issue #581: the financial guard must not withhold a reply
just because a number sits near a financial-sounding word.

Two corpora anchor the behavior:

* operational sentences that use generic nouns (rate, balance, amount,
  estimate, charge, budget, savings, price, cost-as-capability) with counts,
  times, sizes, numbered list markers, Region codes, and availability zones -
  all must be treated as clean; and
* genuine monetary or percentage claims, including amounts split from a
  financial noun by a terminator or abbreviation, amounts after a currency
  code or spaced symbol, and amounts in AWS pricing units - all must stay
  withheld.
"""

# Third-party packages
import pytest

# Local modules
from agents.financial_guard import (
    _neutralize_quantities,
    contains_unvalidated_financial_content,
)

pytestmark = pytest.mark.unit


# Non-financial operational sentences that contain numbers next to generic
# financial-sounding nouns. None of these states a monetary value.
_OPERATIONAL_CLEAN = [
    # The three sentences reported in issue #581.
    "Got it. Battle royale, 40-minute sessions. I can help with GameLift, EKS, and cost questions.",
    "Set the scale-in rate to 2 instances per minute so servers can drain.",
    "You have 3 fleets across 2 regions. Ask me about cost anytime.",
    # Counts, times, and sizes next to generic nouns.
    "The scale-in rate is 2 instances per minute.",
    "Set the request rate to 500 players per fleet.",
    "The current balance of 10 nodes holds across the cluster.",
    "Scale the amount to 6 replicas before the event.",
    "I estimate the migration will take 30 seconds per node.",
    "The health-check estimate completed in 45 seconds.",
    "Each task is charged to 4 GiB of memory headroom.",
    "The budget for this sprint covers 3 clusters.",
    "Savings in restart time reached 20 seconds after tuning.",
    "The price list has 12 instance families to choose from.",
    "Ask me about cost anytime; you have 5 fleets right now.",
    "Charge drain starts after 90 seconds of inactivity.",
    "The matchmaking rate holds 500 players and 10 nodes steady.",
    "A 40-minute session uses 2 instances and 2 regions.",
    "Rate limiting trips at 1000 requests; 4 sessions are active now.",
    # Numbered Markdown list markers, Region codes, and availability zones are
    # structure or identifiers, not amounts, even on a line that mentions cost.
    "1. **Right-size** your fleets to control cost.",
    "Deploy in us-west-2 and us-east-1 to balance latency and cost.",
    "Spread across 3 AZs for resilience; watch cost.",
    # A short cost-topic answer whose numbered items open a list stays clean.
    "## Cost\n\n1. Use Spot\n2. Right-size",
    # A realistic six-item best-practices answer with ordered-list markers,
    # Region codes, counts, and the word "cost" must be returned unchanged.
    (
        "Here are best practices for running game servers on AWS:\n\n"
        "1. **Right-size your fleets** - match instance types to your server's CPU and "
        "memory profile, and review utilization weekly to keep cost under control.\n"
        "2. **Use Spot Instances for interruption-tolerant capacity** - GameLift FleetIQ "
        "and queues can mix Spot and On-Demand fleets so a Spot interruption does not end "
        "active sessions.\n"
        "3. **Deploy across multiple Regions** - place fleets in us-west-2, us-east-1, and "
        "eu-central-1 so players connect to the lowest-latency location.\n"
        "4. **Spread capacity across at least 2 AZs** - multi-AZ fleets keep matches "
        "running if one Availability Zone degrades.\n"
        "5. **Autoscale on demand** - set a target-tracking policy on available game "
        "sessions and keep a buffer of 2 instances per fleet for peak hours.\n"
        "6. **Monitor and alert** - publish session, player, and latency metrics to "
        "CloudWatch and review cost and capacity trends in the Cost Explorer report."
    ),
    # A nested numbered sub-list of three or more items must not make the next
    # outer marker visible; the outer markers continue their own level.
    "1. **Right-size fleets**\n   1. Review CPU\n   2. Review memory\n   3. Review network\n2. **Use Spot** to reduce cost.",
    "1) Right-size\n   1. CPU\n   2. Memory\n   3. Network\n   4. Disk\n2) Use Spot to lower cost.\n3) Review cost.",
    "1. **Right-size fleets** to reduce cost\n   1. Review CPU\n   2. Review memory\n   3. Review network\n2. **Use Spot**",
    # Numbered Markdown headings and bold numbered pseudo-headings are structure.
    # A deployed best-practices answer was withheld solely by "### 4. Control
    # Costs"; the headings keep their own counter, separate from list items.
    (
        "## Best Practices\n\n### 1. Architecture\n- Use multiple Regions.\n\n"
        "### 2. Scaling\n1. Use target tracking.\n2. Keep a buffer.\n3. Review peaks.\n\n"
        "### 3. Reliability\n- Spread across AZs.\n\n### 4. Control Costs\n- Use Spot for stateless capacity."
    ),
    "#### 1. Placement\n- Pick Regions near players.\n#### 2. Optimize Compute Cost (Without Sacrificing Reliability)",
    "**1. Scale on demand**\n- Use target tracking.\n\n**2. Reduce cost**\n- Right-size fleets.",
    "**1.** Scale on demand.\n**2.** Reduce cost with Spot.",
    "__1.__ Scale on demand.\n__2.__ Reduce cost with Spot.",
    # Off-peak scaling text is not a discount.
    "Utilization averages 35% off-peak and 85% at peak.",
    "CPU sits at 25% off peak.",
    "Concurrent players are 30% off their peak.",
    "Keep minimum capacity at 20% off-hours.",
    "Set desired capacity to 25% off-hours and 100% during peak.",
    "Run at 40% off-season.",
    "The fleet is 15% off its target utilization.",
    "Player counts are 40% off their weekend highs.",
    # An uppercase code later in a numbered step is not an amount marker.
    "1. Pre-scale for CNY and watch the error rate.",
    "1. Set USD as the billing currency.",
    # A shell variable after a numbered step is code, not a currency marker.
    "1. Set `$FLEET_ID`, then review the scaling rate.",
    "1. Export `$CLUSTER_NAME` and check the API rate limits.",
    # Spot allocation-strategy names are identifiers, not prices.
    "Use the price-capacity-optimized allocation strategy and keep 20% of capacity On-Demand.",
    "Use `lowest-price` allocation only for stateless pods; keep 10% buffer capacity.",
    # An interior double underscore in an identifier is not emphasis.
    "The `report__cost` field holds 5 rows.",
    # Interior underscores in identifiers are not emphasis.
    "Run `describe_fleet_capacity` for 3 fleets; the cost_report module is unchanged.",
]


# Genuine monetary or percentage claims that must stay withheld.
_FINANCIAL_WITHHELD = [
    "the rate is 0.12 per hour",
    "budget of 500 per month",
    "savings of 30%",
    "costs 1,200 for 3 instances",
    "Spot can save up to 90%",
    "The bill total is 999.00 this cycle",
    "On-demand rate: 0.20 per instance-hour.",
    "The budget is 200",
    "Expected savings are 3.4",
    # A labelled amount whose value follows in the next sentence/block.
    "Monthly cost:\n\n999",
    "Estimated rate:\n\nSee below.\n\n0.20",
    # A sentence terminator or abbreviation must not release the amount: a bare
    # "." (including after an abbreviation), "?", "!", or ";" still binds the
    # financial noun to the amount anywhere on the line.
    "The monthly cost is approx. 1,204 per month.",
    "Your bill is approx. 1,204 this month.",
    "Estimated cost, e.g. 999 for the fleet.",
    "The budget is ca. 500 per month.",
    "The cost (incl. tax) is 1,204.",
    "The cost is est. 1,204 per month.",
    "The cost is i.e. 1,204.",
    "Spend vs. 1,204 last month.",
    "The total cost is... 1,204.",
    "The cost is. 1204",
    "How much will this cost? Roughly 1,204 per month.",
    "What will you spend? About 1,204.",
    "How much can you save? Up to 90%.",
    "What's the discount? 30%.",
    "Spot savings! Up to 90%.",
    "1,204 per month. That is the estimated cost.",
    "Cost for 3 instances. 0.36 total",
    "- Cost. 1,204",
    "EC2 1,204; S3 300; those are your top costs.",
    "Compute 1,204; storage 300; total spend is shown above.",
    "Costs by service; EC2 1,204; S3 300",
    "Savings; 30%",
    "1,204 per month; that is your cost.",
    # A decimal quantity next to the amount must not neutralize away a sentence
    # boundary and let the amount through.
    "The bill for 2.5 hours of GPU time is 1,204.",
    "The estimated cost of 0.5 days of compute is 1,204.",
    "Your spend over 1.5 months came to 1,204.",
    "Savings over 1.5 years reach 30%.",
    # An amount after a currency code or a spaced symbol, or a currency word
    # with a trailing count unit, stays withheld.
    "Your spend is EUR 1,204 month-to-date.",
    "USD 1,204 instances",
    "USD 0.12 hr",
    "$ 5 instances",
    "The price is $ 40 hours of support.",
    "dollars: 250 days",
    # Bare-number pricing phrasings, including AWS pricing units.
    "Spend is 1,204 month-to-date.",
    "Your bill is 1,204 month to date.",
    "Spending is 1,204 year-to-date.",
    "The storage rate is 0.023 GB-month.",
    "The Fargate price is 0.04048 vCPU-hour.",
    "The rate is 0.12 hr.",
    # A list marker or Region code must not hide a real amount on the same line.
    "1. Monthly cost: 1,204",
    "us-west-2 costs 1,204 per month",
    # Bare amounts spelled with spaces before "to date"/"year to date".
    "Your bill is 999 month to date.",
    "Your bill is 1204 month to date.",
    "Spend is 950 month to date.",
    "Spending is 3200 year to date.",
    "Spend: 950 month to date",
    "Monthly cost:\n\n950 month to date",
    # Compound pricing units spelled with a space, and sub-one sizes.
    "The Fargate price is 0.04048 vCPU hour.",
    "The Fargate price is 0.04048 vCPU per hour.",
    "The storage rate is 0.023 GB month.",
    "On-demand rate: 0.20 instance hour.",
    "Each instance costs 0.0116 instance hour",
    "Savings Plan rate 0.0624 vCPU",
    "The storage rate is 0.023 GB.",
    "Data transfer costs 0.09 GB",
    # A line-start value that ends a sentence is not a list marker and must stay.
    "999. That is the estimated monthly cost.",
    "450. That assumes on-demand rates.",
    "Monthly cost:\n\n450. Most of that is EC2.",
    "Monthly cost:\n450. EC2 is the largest line item.",
    "**Estimated monthly cost:**\n\n120. This assumes On-Demand pricing.",
    "Budget:\n\n500) per month",
    "Total spend\n999) for the fleet",
    # A numbered heading never hides an amount or savings claim on its line, and
    # a heading number that does not continue a heading sequence stays visible.
    "### 1. Architecture\n### 2. Control Costs\n- Spot saves up to 90% of the On-Demand price.",
    "### 1. Overview\n### 2. Save 90% with Spot",
    "__450.__ That assumes on-demand rates.",
    # Plain list markers and heading-like markers keep separate counters, so one
    # kind of sequence cannot prime the other into hiding a value.
    "1. a\n2. b\n3. c\n**4.** That is the estimated monthly cost.",
    "### 1. a\n### 2. b\n### 3. c\n4. That is the estimated monthly cost.",
    # Unpaired or mismatched bold delimiters are not a heading marker.
    "1.** Monthly cost",
    "**1.__ Monthly cost",
    # A currency word right after a marker keeps the marker's digits visible.
    "**1.** dollars per hour",
    "### 1. dollars per hour",
    "1. dollars per hour",
    # Savings, discount, and price-comparison percentages are financial claims.
    "Spot saves up to 90% of the On-Demand price.",
    "Spot is up to 90% cheaper than On-Demand.",
    "### 3. Use Spot (up to 90% off)",
    "Graviton offers up to 40% better price performance.",
    "Savings Plans can lower your compute bills by 72%.",
    # Underscore emphasis around a financial word does not hide it.
    "_Savings_: 30%",
    "The _monthly cost_ is 1,204.",
    "The __cost__ is 1,204 per month.",
    "__Monthly cost__: 1,204",
    "__Monthly cost:__\n1,204",
    "_Savings:_\n30%",
    "Run the bash script; the _estimated cost_ is $5 per run.",
    "1. _dollars_ per hour",
    "1. Compute\n2. USD per month",
    # Each widened savings and price-comparison word is pinned on its own.
    "Spot saves 90%.",
    "Reserved capacity discounts reach 40%.",
    "Graviton lowers your bills by 20%.",
    "Spot is the cheapest option at up to 90% below On-Demand.",
    "On-Demand is 90% more expensive than Spot.",
    "Spot prices are up to 90% lower.",
    "Spot pricing is up to 90% lower.",
    "Spot saves up to 90 percent compared to On-Demand.",
    "Spot: up to 90% off On-Demand.",
    "Up to 90% off Linux On-Demand instances.",
    "Spot is 90% off their normal rate.",
    # "lowest-price" as an adjective still names a bare price.
    "Karpenter picked the lowest-price pool, m6g.large, at 0.0385 per hour.",
    "Lowest-price: 0.077 per hour",
    "The lowest-price Spot pool in us-west-2 currently trades at 0.0312 per hour.",
    # Underscore emphasis in each window the quantity and label checks read.
    "_**Monthly cost**_\n1,204",
    "In _dollars_, that is about 5 instances",
    "4 GiB in _dollars_",
    "Total _USD_" + " " * 60 + "5 instances",
    "### 1. Cost: $1,200 per month",
    "### 1. Overview\n### 2. Monthly cost: 1,204",
    "### 999. That is the estimated monthly cost.",
    "**450.** That assumes on-demand rates.",
    # A currency word within twenty characters after the quantity preserves it.
    "0.023 GB in dollars",
    "Storage: 0.023 GB in dollars",
    "dollars, about 250 instances",
    "In dollars, that is about 5 instances",
    "$                    5 instances",
    "Spend in euros so far: 1204 month to date",
    # An "xx-word-digit" token that is not a real Region code must not shield an
    # amount from the financial-noun link.
    "The cost is up-to-5 per hour.",
    "Savings are up-to-5 per hour.",
    "The cost is ab-c-5",
    "The cost is ab-cd-9.",
    "The cost is xx-yy-9 per hour.",
    # Period-over-period wording and Unicode hyphens after a time word read as a
    # running total or a compound pricing unit, not an operational quantity.
    "Spend rose by 950 month over month.",
    "Your bill grew 1204 year over year.",
    "Costs increased 300 month on month.",
    "Spend is 950 week over week.",
    "Spend was 950 month to month.",
    "Spend is 950 month\u2011to\u2011date.",
    "Spend is 950 month\u2013to\u2013date.",
    "The storage rate is 1.5 GB\u2011month.",
    "The Fargate price is 2 vCPU\u2011hour.",
    # A line-start number that does not open a list must not set the list counter
    # and let a later real amount through as a list continuation.
    "Fleet IDs:\n449. placeholder line\n" + "x" * 210 + "\n\nMonthly cost:\n\n450. Most of that is EC2.",
    # Decimal-comma amounts must not pass the integer-only count rule or the
    # leading-zero size rule.
    "Savings Plan rate 0,0624 vCPU",
    "The storage rate is 0,023 GB.",
    "The storage rate is 00.023 GB.",
    # A symbol or ISO code followed by forty or more whitespace characters still
    # marks the following number as monetary.
    "$" + " " * 40 + "5 instances",
    "USD" + " " * 40 + "5 instances",
]


class TestOperationalNumbersStayClean:
    @pytest.mark.parametrize("text", _OPERATIONAL_CLEAN)
    def test_operational_sentence_is_not_financial(self, text):
        assert contains_unvalidated_financial_content(text) is False


class TestRealMonetaryClaimsStayWithheld:
    @pytest.mark.parametrize("text", _FINANCIAL_WITHHELD)
    def test_real_amount_is_financial(self, text):
        assert contains_unvalidated_financial_content(text) is True


# Comma-grouped integer counts and sizes remain operational quantities: the
# quantity neutralizer blanks their digits so they cannot bind to a nearby
# financial word.
_NEUTRALIZED_QUANTITIES = [
    "4,096 requests",
    "10,000 players",
    "1,204 instances",
    "0 GB",
]


class TestCommaGroupedQuantitiesStayNeutralized:
    @pytest.mark.parametrize("fragment", _NEUTRALIZED_QUANTITIES)
    def test_quantity_digits_are_blanked(self, fragment):
        assert any(ch.isdigit() for ch in fragment)
        assert not any(ch.isdigit() for ch in _neutralize_quantities(fragment))
