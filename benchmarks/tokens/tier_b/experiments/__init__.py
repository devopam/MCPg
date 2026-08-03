"""One-off Tier-B diagnostics — not part of the measured/committed study.

Each script here is a standalone, reusable investigation that deliberately
deviates from the default runner's methodology (e.g. forcing a tool that the
model wouldn't otherwise choose) to answer a specific question, rather than
producing a published number. Never run in CI; never wired into
``benchmarks.tokens.tier_b.runner``.
"""
