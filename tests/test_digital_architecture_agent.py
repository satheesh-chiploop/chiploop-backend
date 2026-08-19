import json
import os

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital import digital_architecture_agent as agent


def test_architecture_parser_accepts_literal_newline_inside_json_string():
    parsed = agent._parse_architecture_json('{"intent":"Latch\nfaults","modules":[]}')

    assert parsed == {"intent": "Latch\nfaults", "modules": []}


def test_architecture_parser_rejects_non_object_json():
    with pytest.raises(ValueError, match="one JSON object"):
        agent._parse_architecture_json(json.dumps(["not", "an", "object"]))


def test_architecture_parser_does_not_hide_structural_json_errors():
    with pytest.raises(json.JSONDecodeError):
        agent._parse_architecture_json('{"intent" "missing colon"}')
