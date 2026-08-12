import os

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.digital.digital_register_map_agent import _register_layout_violations


def test_register_layout_rejects_fields_beyond_declared_bus_width():
    document = {
        "regmap": {
            "data_width": 64,
            "registers": [{
                "name": "CTRL_STATUS",
                "offset": "0x0",
                "fields": [
                    {"name": "CONTROL", "lsb": 0, "msb": 29},
                    {"name": "STATUS", "lsb": 69, "msb": 79},
                ],
            }],
        },
    }

    violations = _register_layout_violations(document)

    assert violations == ["CTRL_STATUS.STATUS [79:69] is outside the 64-bit register word"]


def test_register_layout_accepts_multiple_addressed_words():
    document = {
        "regmap": {
            "data_width": 64,
            "registers": [
                {"name": "CONTROL", "offset": "0x0", "fields": [{"name": "ENABLE", "lsb": 0, "msb": 0}]},
                {"name": "STATUS", "offset": "0x8", "fields": [{"name": "READY", "lsb": 0, "msb": 0}]},
            ],
        },
    }

    assert _register_layout_violations(document) == []


def test_register_layout_rejects_overlapping_fields():
    document = {
        "regmap": {
            "data_width": 32,
            "registers": [{
                "name": "CONTROL",
                "offset": "0x0",
                "fields": [
                    {"name": "MODE", "lsb": 0, "msb": 3},
                    {"name": "ENABLE", "lsb": 3, "msb": 3},
                ],
            }],
        },
    }

    assert _register_layout_violations(document) == ["CONTROL.ENABLE [3:3] overlaps MODE [3:0]"]
