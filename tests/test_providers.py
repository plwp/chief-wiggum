from __future__ import annotations

import json

import providers
import pytest


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


# --- review lenses (chief-wiggum#163) ---------------------------------------


def test_default_lenses_config_is_valid():
    lenses = providers.load_lenses()
    assert {"refute-soundness", "adoption-cost", "completeness", "security"} <= set(lenses)
    for name, lens in lenses.items():
        assert lens.get("goal"), f"lens {name} has no goal"
        assert lens.get("exclusions"), f"lens {name} has no exclusions"


def test_load_lenses_missing_file_returns_empty_mapping(tmp_path):
    assert providers.load_lenses(tmp_path / "does-not-exist.json") == {}


def test_render_charter_includes_goal_and_exclusions():
    charter = providers.render_charter(
        {"goal": "Break the reasoning.", "exclusions": ["Do NOT evaluate style."]}
    )
    assert charter.startswith("## Your charter")
    assert "Break the reasoning." in charter
    assert "- Do NOT evaluate style." in charter


def test_prompt_for_provider_returns_shared_prompt_unchanged_when_unmapped():
    role = providers.Role(name="reviewer", required=("codex",), optional=())
    assert providers.prompt_for_provider(role, "codex", "shared body", {}) == "shared body"


def test_prompt_for_provider_appends_charter_for_mapped_provider():
    role = providers.Role(
        name="reviewer", required=("codex",), optional=(), lenses={"codex": "refute-soundness"}
    )
    lenses = {"refute-soundness": {"goal": "Break it.", "exclusions": ["Do NOT nitpick style."]}}

    result = providers.prompt_for_provider(role, "codex", "shared body", lenses)

    assert result.startswith("shared body")
    assert "## Your charter" in result
    assert "Break it." in result


def test_prompt_for_provider_raises_for_unknown_lens():
    role = providers.Role(
        name="reviewer", required=("codex",), optional=(), lenses={"codex": "no-such-lens"}
    )
    with pytest.raises(KeyError):
        providers.prompt_for_provider(role, "codex", "shared body", {})


def test_validate_lenses_flags_unknown_lens_name():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": [], "lenses": {"codex": "missing-lens"}}},
    }
    errors = providers.validate_lenses(config, {"refute-soundness": {}})
    assert any("unknown lens" in e for e in errors)


def test_validate_lenses_flags_provider_not_in_role():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": [], "lenses": {"gemini": "refute-soundness"}}},
    }
    errors = providers.validate_lenses(config, {"refute-soundness": {}})
    assert any("not a required or optional provider" in e for e in errors)


def test_validate_lenses_passes_for_well_formed_role():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": [], "lenses": {"codex": "refute-soundness"}}},
    }
    assert providers.validate_lenses(config, {"refute-soundness": {}}) == []


# --- optional-provider timeout knob (chief-wiggum#188) ----------------------
#
# claude-interactive timed out at its full 1800s budget on two consecutive
# large-prompt consults while contributing nothing (it is optional in every
# shipped role) — a role's optional_timeout_seconds caps how long the
# quorum lets an OPTIONAL provider's delegate call run before abandoning it,
# so the required providers' wall-clock is never held hostage to a voice
# that's allowed to fail.


def test_role_loads_optional_timeout_seconds_from_config():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {
            "reviewer": {
                "required": ["codex"], "optional": [], "optional_timeout_seconds": 300,
            }
        },
    }
    role = providers.roles_from_config(config)["reviewer"]
    assert role.optional_timeout_seconds == 300


def test_role_optional_timeout_seconds_defaults_to_none_when_absent():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {"reviewer": {"required": ["codex"], "optional": []}},
    }
    role = providers.roles_from_config(config)["reviewer"]
    assert role.optional_timeout_seconds is None


@pytest.mark.parametrize("bad_value", [0, -5, "300", 3.5, True])
def test_validate_config_rejects_malformed_optional_timeout_seconds(bad_value):
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {
            "reviewer": {
                "required": ["codex"], "optional": [], "optional_timeout_seconds": bad_value,
            }
        },
    }
    errors = providers.validate_config(config)
    assert any("invalid optional_timeout_seconds" in e for e in errors)


def test_validate_config_accepts_well_formed_optional_timeout_seconds():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {
            "reviewer": {
                "required": ["codex"], "optional": [], "optional_timeout_seconds": 300,
            }
        },
    }
    assert providers.validate_config(config) == []


def test_default_provider_config_sets_optional_timeout_seconds_on_every_role():
    # Every shipped role includes claude-interactive as optional (chief-wiggum#188);
    # each must set the knob so the delegate never silently reverts to its full
    # 1800s budget in the optional slot.
    config = providers.load_config()
    for name, role in providers.roles_from_config(config).items():
        assert role.optional_timeout_seconds is not None, (
            f"role {name} has no optional_timeout_seconds — its optional "
            "claude-interactive call would fall back to the DEFAULT constant, "
            "which is fine functionally but should be explicit in shipped config"
        )
