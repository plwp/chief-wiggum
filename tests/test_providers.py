from __future__ import annotations

import json

import providers
import pytest


def test_default_provider_config_is_valid():
    config = providers.load_config()

    assert providers.validate_config(config) == []
    roles = providers.roles_from_config(config)
    assert {
        "explorer",
        "implementer",
        "reviewer",
        "architecture_critic",
        "design_critic",
        "risky_diff_review",
    } <= set(roles)


def test_default_config_has_disabled_external_preview_execution_provider():
    config = providers.load_config()
    execution = providers.execution_providers_from_config(config)

    provider = execution["openrouter-preview-worker"]
    assert not provider.enabled
    assert provider.execution_adapter == "codex-responses"
    assert provider.capability_tier == "external-preview-tier"
    assert all(
        "openrouter-preview-worker" not in raw.get("required", []) + raw.get("optional", [])
        for raw in config["roles"].values()
    )


def test_anonymous_preview_without_license_must_be_external_preview_tier():
    config = {
        "providers": {
            "preview": {
                "type": "delegate",
                "delegate": "codex-responses",
                "execution_adapter": "codex-responses",
                "model": "configured/model",
                "enabled": False,
                "base_url": "https://example.invalid/api/v1",
                "capability_tier": "open-tier",
                "capabilities": ["responses", "shell-tools"],
                "anonymous_preview": True,
                "weights_license_evidence": None,
            }
        },
        "roles": {},
    }

    errors = providers.validate_config(config)
    assert any("external-preview-tier" in error for error in errors)


def test_open_tier_without_license_evidence_is_rejected_when_preview_flag_omitted():
    config = {
        "providers": {
            "unproven": {
                "type": "delegate",
                "delegate": "codex-responses",
                "execution_adapter": "codex-responses",
                "model": "configured/model",
                "enabled": False,
                "base_url": "https://example.invalid/api/v1",
                "capability_tier": "open-tier",
                "capabilities": ["responses", "repo-read", "shell-tools", "workspace-write"],
                "weights_license_evidence": None,
            }
        },
        "roles": {},
    }

    assert any("cannot use open-tier" in error for error in providers.validate_config(config))


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

    assert providers.validate_config(config) == [
        "role reviewer references unknown provider missing"
    ]


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
        "roles": {
            "reviewer": {"required": ["codex"], "optional": [], "lenses": {"codex": "missing-lens"}}
        },
    }
    errors = providers.validate_lenses(config, {"refute-soundness": {}})
    assert any("unknown lens" in e for e in errors)


def test_validate_lenses_flags_provider_not_in_role():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {
            "reviewer": {
                "required": ["codex"],
                "optional": [],
                "lenses": {"gemini": "refute-soundness"},
            }
        },
    }
    errors = providers.validate_lenses(config, {"refute-soundness": {}})
    assert any("not a required or optional provider" in e for e in errors)


def test_validate_lenses_passes_for_well_formed_role():
    config = {
        "providers": {"codex": {"type": "tool", "tool": "codex"}},
        "roles": {
            "reviewer": {
                "required": ["codex"],
                "optional": [],
                "lenses": {"codex": "refute-soundness"},
            }
        },
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
                "required": ["codex"],
                "optional": [],
                "optional_timeout_seconds": 300,
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
                "required": ["codex"],
                "optional": [],
                "optional_timeout_seconds": bad_value,
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
                "required": ["codex"],
                "optional": [],
                "optional_timeout_seconds": 300,
            }
        },
    }
    assert providers.validate_config(config) == []


def _models_md() -> str:
    from pathlib import Path

    # Explicit encoding: the file carries em dashes, and read_text() would
    # otherwise decode by locale, which is a portability trap rather than a
    # today problem.
    return (Path(__file__).resolve().parents[1] / "models.md").read_text(
        encoding="utf-8")


def _openrouter_section() -> str:
    """Just the OpenRouter table, so other vendors' slugs are not swept in."""
    text = _models_md()
    start = text.index("## OpenRouter")
    rest = text.index("\n## ", start + 1)
    return text[start:rest]


def _tabled_slugs() -> set[str]:
    """The Model ID column of the OpenRouter table, lowercased.

    Read from the table's second column rather than by pattern-matching the
    prose: a "looks like a slug" heuristic also matches `config/providers.json`
    and `scripts/consult_ai.py`, which is what the first attempt at this did.
    """
    slugs = set()
    for line in _openrouter_section().splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        cell = cells[2].strip()
        if cell.startswith("`") and cell.endswith("`"):
            slugs.add(cell.strip("`").strip().lower())
    return slugs


def _routed_openrouter_models(config: dict | None = None) -> dict[str, str]:
    """Enabled OpenRouter-backed providers, name -> model slug.

    A provider switched off is deliberately not dispatched to, so requiring a
    reference entry for it would fail CI over a config change that broke
    nothing — and a guard that cries wolf teaches the operator to bypass it.
    """
    config = config if config is not None else providers.load_config()
    return {
        name: spec["model"]
        for name, spec in (config.get("providers") or {}).items()
        if spec.get("tool") == "openrouter" and spec.get("model")
        and spec.get("enabled", True)
    }


def test_every_openrouter_model_slug_is_documented_in_models_md():
    """models.md must list every OpenRouter-backed provider (chief-wiggum#368).

    These are ROUTING SLUGS, not vendor IDs, and they change independently of
    the vendor's own naming. `config/providers.json` is what dispatches;
    models.md is what a human reads when choosing a voice, and `/update` is
    what refreshes both. A provider wired into a role but absent from the
    reference is one nobody knows to re-check when the slug moves — and a
    stale slug does not degrade gracefully, it 404s and that reviewer drops
    out of its role.
    """
    documented = _tabled_slugs()
    routed = _routed_openrouter_models()
    assert routed, "expected at least one openrouter-backed provider to exist"
    assert documented, "the OpenRouter table in models.md has no model column"

    missing = {name: model for name, model in routed.items()
               if model.lower() not in documented}
    assert not missing, (
        f"models.md does not document these OpenRouter model slugs: {missing}. "
        "Add them, or drop the provider from config/providers.json."
    )


def test_a_disabled_provider_is_not_required_to_be_documented():
    """Switching a provider off must not break CI over the reference.

    Nothing dispatches to a disabled provider, so demanding a models.md entry
    for it would fail the build over a config change that broke nothing — and
    a guard that cries wolf teaches the operator to bypass it. Exercised on a
    synthetic config because no shipped OpenRouter provider is currently
    disabled, which would otherwise leave this filter untested.
    """
    config = {"providers": {
        "live": {"tool": "openrouter", "model": "vendor/live-v1", "enabled": True},
        "off": {"tool": "openrouter", "model": "vendor/off-v1", "enabled": False},
        "implied": {"tool": "openrouter", "model": "vendor/implied-v1"},
        "other-tool": {"tool": "codex", "model": "vendor/not-routed"},
    }}
    assert _routed_openrouter_models(config) == {
        "live": "vendor/live-v1",
        "implied": "vendor/implied-v1",  # absent `enabled` means enabled
    }


def test_models_md_does_not_advertise_a_provider_that_is_no_longer_wired():
    """The reverse drift, which the forward check cannot see.

    A slug left in the reference after its provider was removed from config
    tells a reader the model is available when nothing dispatches to it. Only
    the OpenRouter section is scanned, so slugs belonging to other vendors are
    not swept up.
    """
    slugs = _tabled_slugs()
    routed = {model.lower() for model in _routed_openrouter_models().values()}

    stale = sorted(slugs - routed)
    assert not stale, (
        f"models.md advertises OpenRouter slugs nothing dispatches to: {stale}. "
        "Remove them, or wire the provider back into config/providers.json."
    )


def test_models_md_role_membership_claims_match_the_config():
    """The prose claims are drift too, and were the untested half.

    models.md states which roles each provider sits in and whether it is
    required there. That is exactly the kind of hand-maintained assertion this
    file's slug check exists to stop trusting — raised in review, and fair:
    guarding the slugs while leaving the surrounding sentences unguarded is
    half a guard.
    """
    config = providers.load_config()
    roles = config.get("roles") or {}

    def membership(provider: str) -> tuple[set[str], set[str]]:
        required = {name for name, role in roles.items()
                    if provider in (role.get("required") or [])}
        optional = {name for name, role in roles.items()
                    if provider in (role.get("optional") or [])}
        return required, optional

    # The claims made in models.md's OpenRouter table, transcribed.
    claimed = {
        "deepseek-flash": ({"reviewer", "risky_diff_review"},
                           {"explorer", "architecture_critic"}),
        "deepseek": ({"divergence"}, set()),
        "kimi": ({"divergence"}, set()),
        "glm": (set(), {"divergence"}),
        "qwen": (set(), {"divergence"}),
        "minimax": (set(), {"divergence"}),
    }
    for provider, (want_required, want_optional) in claimed.items():
        got_required, got_optional = membership(provider)
        assert got_required == want_required, (
            f"models.md says {provider} is required in {sorted(want_required)},"
            f" config says {sorted(got_required)}")
        assert got_optional == want_optional, (
            f"models.md says {provider} is optional in {sorted(want_optional)},"
            f" config says {sorted(got_optional)}")


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
