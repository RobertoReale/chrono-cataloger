from src.costs import CostTracker, estimate_cost_usd, estimate_tokens


def test_estimate_tokens_never_zero():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100


def test_unknown_model_has_no_price():
    assert estimate_cost_usd("some-local-llama", 1000, 1000) is None


def test_metered_provider_reports_cost():
    t = CostTracker("anthropic")
    t.add("claude-sonnet-5", 1_000_000, 0)
    d = t.as_dict()
    assert d["estimated_cost_usd"] == 3.0


def test_subscription_provider_reports_no_cost():
    """claude_code is not billed per token: a dollar figure would be invented."""
    t = CostTracker("claude_code")
    t.add("claude-sonnet-5", 1_000_000, 1_000_000)
    d = t.as_dict()
    assert d["estimated_cost_usd"] is None
    assert d["estimated_input_tokens"] == 1_000_000
    assert "not billed per token" in d["cost_note"]


def test_local_provider_reports_no_cost():
    t = CostTracker("ollama")
    t.add("llama3", 5000, 5000)
    assert t.as_dict()["estimated_cost_usd"] is None


def test_unpriced_model_marks_total_as_lower_bound():
    t = CostTracker("openai")
    t.add("gpt-nonexistent", 1_000_000, 0)
    d = t.as_dict()
    assert d["estimated_cost_usd"] == 0.0
    assert "lower bound" in d["cost_note"]
