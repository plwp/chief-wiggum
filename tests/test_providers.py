from __future__ import annotations

import json

import providers


def test_default_provider_config_is_valid():
    config = providers.load_config()

    assert providers.validate_config(config) == []
    roles = providers.roles_from_config(config)
    assert {"explorer", "implementer", "reviewer", "architecture_critic", "design_critic", "risky_diff_review"} <= set(roles)


def test_role_plan_separates_required_optional_and_disabled():
    config = {
        "providers": {
            "codex": {"type": "tool", "tool": "codex", "enabled": True},
            "gemini": {"type": "tool", "tool": "gemini", "enabled": True},
            "claude-interactive": {
                "type": "delegate",
                "delegate": "claude-interactive",
                "enabled": True,
            },
        },
        "roles": {
            "reviewer": {
                "required": ["codex", "gemini"],
                "optional": ["claude-interactive"],
            }
        },
    }

    plan = providers.plan_role("reviewer", config, disabled={"claude-interactive"})

    assert plan.ok
    assert [provider.name for provider in plan.required] == ["codex", "gemini"]
    assert plan.optional == ()
    assert plan.skipped_optional == ("claude-interactive",)


def test_required_provider_can_be_disabled_and_makes_plan_not_ok():
    config = {
        "providers": {
            "codex": {"type": "tool", "tool": "codex", "enabled": True},
            "gemini": {"type": "tool", "tool": "gemini", "enabled": True},
        },
        "roles": {"reviewer": {"required": ["codex", "gemini"], "optional": []}},
    }

    plan = providers.plan_role("reviewer", config, disabled={"gemini"})

    assert not plan.ok
    assert plan.missing_required == ("gemini",)


def test_validate_config_flags_unknown_role_provider():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": ["missing"]}},
    }

    assert providers.validate_config(config) == ["role reviewer references unknown provider missing"]


def test_validate_config_can_flag_unknown_backend_names():
    config = {
        "providers": {
            "bad-tool": {"type": "tool", "tool": "bogus"},
            "bad-delegate": {"type": "delegate", "delegate": "bogus"},
        },
        "roles": {},
    }

    assert providers.validate_config(
        config,
        supported_tools={"codex"},
        supported_delegates={"claude-interactive"},
    ) == [
        "provider bad-tool references unsupported tool bogus",
        "provider bad-delegate references unsupported delegate bogus",
    ]


def test_config_round_trips_from_json_file(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": {"codex": {"type": "tool", "tool": "codex"}},
                "roles": {"reviewer": {"required": ["codex"], "optional": []}},
            }
        )
    )

    assert providers.load_config(path)["roles"]["reviewer"]["required"] == ["codex"]
