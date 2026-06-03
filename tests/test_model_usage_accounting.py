from model_gateway.usage import record_model_usage, usage_counts


class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def insert(self, row):
        self.client.rows.setdefault(self.name, []).append(row)
        self.row = row
        return self

    def execute(self):
        if self.name == "model_usage_events":
            return _Result([{"id": "usage-1"}])
        return _Result([])


class _Client:
    def __init__(self):
        self.rows = {}

    def table(self, name):
        return _Table(self, name)


def test_usage_counts_prefers_provider_usage():
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert usage_counts("hello", "world", usage) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }


def test_record_model_usage_byok_records_event_only(monkeypatch):
    client = _Client()
    monkeypatch.setattr("model_gateway.usage.get_platform_client", lambda: client)
    record_model_usage(
        state={"user_id": "u1", "workflow_id": "wf1", "run_id": "run1"},
        profile={"ai_billing_mode": "byok", "model_key_owner": "customer"},
        route={"model": "gpt-5.5"},
        provider="openai",
        model="gpt-5.5",
        capability="rtl_generation",
        agent_name="Digital RTL Agent",
        prompt="abcd",
        output="efgh",
        provider_usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    )
    assert len(client.rows["model_usage_events"]) == 1
    assert "org_credit_ledger" not in client.rows
    row = client.rows["model_usage_events"][0]
    assert row["ai_billing_mode"] == "byok"
    assert row["model_key_owner"] == "customer"
    assert row["total_tokens"] == 5


def test_record_model_usage_chiploop_managed_records_credit_ledger(monkeypatch):
    client = _Client()
    monkeypatch.setattr("model_gateway.usage.get_platform_client", lambda: client)
    record_model_usage(
        state={"org_id": "org1", "user_id": "u1", "workflow_id": "wf1", "run_id": "run1"},
        profile={"ai_billing_mode": "chiploop_managed", "model_key_owner": "chiploop"},
        route={"credits_per_1k_tokens": 2},
        provider="openai",
        model="gpt-5.5",
        capability="spec_generation",
        agent_name="Digital Spec Agent",
        prompt="a" * 4000,
        output="b" * 4000,
    )
    assert len(client.rows["model_usage_events"]) == 1
    assert len(client.rows["org_credit_ledger"]) == 1
    ledger = client.rows["org_credit_ledger"][0]
    assert ledger["event_type"] == "model_usage"
    assert ledger["credits_delta"] < 0
    assert ledger["model_usage_event_id"] == "usage-1"
