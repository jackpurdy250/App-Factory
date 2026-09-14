"""Configuration, provider presets, and credentials.

This module is the proof behind the claim that App Factory talks to any
provider that speaks an OpenAI- or Anthropic-shaped HTTP API. Nothing here
reaches the network: every case builds a models.toml in a temp directory and
asserts on what the loader resolved.

The rule these tests defend is that a vendor name may appear in config only.
If a provider ever has to be special-cased in core/, this suite should be the
thing that makes that obvious.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import (  # noqa: E402
    NO_CREDENTIAL,
    PRESETS_FILENAME,
    PROVIDER_KINDS,
    AppConfig,
    ConfigError,
    ConfigStore,
    _load_presets,
    load_config,
)
from core.schemas.common import AgentRole  # noqa: E402
from tests.harness import (  # noqa: E402
    CONFIG_DIR,
    GOVERNOR,
    MOCK_MODELS,
    PRESETS,
    Checker,
    discard,
    temp_root,
)

ROLE_SOURCE = MOCK_MODELS.read_text()


def role_section(provider_name: str) -> str:
    """The seven role blocks from the mock config, pointed at `provider_name`.

    Reusing the real role section keeps these cases honest: if a role gains a
    required key, every case here starts exercising it automatically.
    """
    index = ROLE_SOURCE.index("[roles.")
    # The real config aligns its `=` signs, so a plain string replace would
    # silently match nothing. Tolerate any spacing.
    return re.sub(
        r'provider(\s*)=(\s*)"mock"',
        lambda match: f'provider{match.group(1)}={match.group(2)}"{provider_name}"',
        ROLE_SOURCE[index:],
    )


def models_doc(provider_blocks: str, *, provider_name: str) -> str:
    return provider_blocks.rstrip() + "\n\n" + role_section(provider_name)


def load_case(
    root: Path,
    name: str,
    doc: str,
    *,
    with_presets: bool = True,
) -> AppConfig:
    case_dir = root / name
    case_dir.mkdir(parents=True, exist_ok=True)
    models_path = case_dir / "models.toml"
    models_path.write_text(doc)
    if with_presets:
        shutil.copy(PRESETS, case_dir / PRESETS_FILENAME)
    return load_config(
        models_path, GOVERNOR, env_path=None, runs_dir=case_dir / "projects"
    )


def preset_field(preset: Any, key: str) -> Any:
    if isinstance(preset, dict):
        return preset.get(key)
    return getattr(preset, key, None)


def provider_names(chain: Any) -> list[str]:
    return [getattr(item, "name", item) for item in chain]


def error_text(call) -> str:
    try:
        call()
    except ConfigError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced in the assertion
        return f"WRONG-EXCEPTION {type(exc).__name__}: {exc}"
    return "NO-ERROR"


def run() -> tuple[int, int]:
    check = Checker("test_config")
    root = temp_root("af-config-")

    try:
        # -- the preset catalogue -----------------------------------------

        check.section("provider presets")

        presets = _load_presets(CONFIG_DIR / "models.toml")
        expected = {
            "openai",
            "anthropic",
            "google",
            "xai",
            "deepseek",
            "mistral",
            "groq",
            "cerebras",
            "together",
            "fireworks",
            "openrouter",
            "perplexity",
            "moonshot",
            "qwen",
            "zhipu",
            "ollama",
            "lmstudio",
            "vllm",
            "llamacpp",
        }
        check.equal("preset catalogue has 19 entries", len(presets), 19)
        check.equal("preset names are exactly as documented", set(presets), expected)

        for name, preset in sorted(presets.items()):
            check.contains(
                f"preset {name} declares a known wire format",
                PROVIDER_KINDS,
                preset_field(preset, "kind"),
            )
        for name in sorted(presets):
            check.truthy(
                f"preset {name} declares a base_url",
                preset_field(presets[name], "base_url"),
            )

        for name in ("ollama", "lmstudio", "vllm", "llamacpp"):
            check.equal(
                f"local preset {name} needs no credential",
                preset_field(presets[name], "api_key"),
                NO_CREDENTIAL,
            )

        check.excludes(
            "azure is deliberately absent (it needs an api-key header)",
            set(presets),
            "azure",
        )
        check.contains(
            "google is reached through its OpenAI-compatible endpoint",
            preset_field(presets["google"], "base_url"),
            "generativelanguage",
        )
        check.contains(
            "zhipu (GLM) is reached through its OpenAI-compatible endpoint",
            preset_field(presets["zhipu"], "base_url"),
            "bigmodel",
        )

        # -- presets resolve into providers -------------------------------

        check.section("preset resolution")

        hosted = load_case(
            root,
            "hosted",
            models_doc(
                '[provider.p1]\npreset = "openai"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
        )
        p1 = hosted.provider("p1")
        check.equal("openai preset sets the wire format", p1.kind, "openai_compatible")
        check.equal(
            "openai preset sets the base url", p1.base_url, "https://api.openai.com/v1"
        )
        check.equal("declared api_key is kept", p1.api_key, "sk-test")
        check.falsy("a real provider is not the mock", p1.is_mock)

        anthropic = load_case(
            root,
            "anthropic",
            models_doc(
                '[provider.p1]\npreset = "anthropic"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
        )
        check.equal(
            "anthropic preset selects the messages wire format",
            anthropic.provider("p1").kind,
            "anthropic_compatible",
        )

        local = load_case(
            root,
            "local",
            models_doc('[provider.p1]\npreset = "ollama"\n', provider_name="p1"),
        )
        check.equal(
            "a local preset implies no credential",
            local.provider("p1").api_key,
            NO_CREDENTIAL,
        )
        check.contains(
            "a local preset points at localhost",
            local.provider("p1").base_url,
            "localhost",
        )

        overridden = load_case(
            root,
            "override-url",
            models_doc(
                '[provider.p1]\npreset = "openai"\n'
                'base_url = "https://gateway.internal/v1"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
        )
        check.equal(
            "an explicit base_url beats the preset",
            overridden.provider("p1").base_url,
            "https://gateway.internal/v1",
        )
        check.equal(
            "the preset still supplies the wire format",
            overridden.provider("p1").kind,
            "openai_compatible",
        )

        kind_override = load_case(
            root,
            "override-kind",
            models_doc(
                '[provider.p1]\npreset = "openai"\n'
                'kind = "anthropic_compatible"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
        )
        check.equal(
            "an explicit kind beats the preset",
            kind_override.provider("p1").kind,
            "anthropic_compatible",
        )

        no_presets_file = load_case(
            root,
            "no-presets",
            models_doc(
                '[provider.p1]\nkind = "openai_compatible"\n'
                'base_url = "https://api.example.com/v1"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
            with_presets=False,
        )
        check.equal(
            "a missing presets file is not an error when kind is explicit",
            no_presets_file.provider("p1").kind,
            "openai_compatible",
        )

        unknown_preset = error_text(
            lambda: load_case(
                root,
                "unknown-preset",
                models_doc(
                    '[provider.p1]\npreset = "chatgpt5"\napi_key = "sk-test"\n',
                    provider_name="p1",
                ),
            )
        )
        check.contains(
            "an unknown preset names the offending preset", unknown_preset, "chatgpt5"
        )

        neither = error_text(
            lambda: load_case(
                root,
                "neither",
                models_doc(
                    '[provider.p1]\nbase_url = "https://x/v1"\napi_key = "k"\n',
                    provider_name="p1",
                ),
            )
        )
        check.contains(
            "a provider with no preset and no kind is refused", neither, "preset"
        )

        # -- credentials ---------------------------------------------------

        check.section("credentials")

        missing_key = error_text(
            lambda: load_case(
                root,
                "missing-key",
                models_doc('[provider.p1]\npreset = "openai"\n', provider_name="p1"),
            )
        )
        check.contains(
            "a hosted provider with no api_key fails at startup", missing_key, "api_key"
        )
        check.contains(
            "the failure names the provider block", missing_key, "p1"
        )

        explicit_kind_no_key = error_text(
            lambda: load_case(
                root,
                "explicit-no-key",
                models_doc(
                    '[provider.p1]\nkind = "openai_compatible"\n'
                    'base_url = "https://api.example.com/v1"\n',
                    provider_name="p1",
                ),
            )
        )
        check.contains(
            "an explicit kind still requires a credential decision",
            explicit_kind_no_key,
            "api_key",
        )

        opted_out = load_case(
            root,
            "opted-out",
            models_doc(
                '[provider.p1]\nkind = "openai_compatible"\n'
                'base_url = "http://127.0.0.1:9000/v1"\n'
                f'api_key = "{NO_CREDENTIAL}"\n',
                provider_name="p1",
            ),
        )
        check.equal(
            "opting out of credentials explicitly is allowed",
            opted_out.provider("p1").api_key,
            NO_CREDENTIAL,
        )

        mock_cfg = load_config(
            MOCK_MODELS, GOVERNOR, env_path=None, runs_dir=root / "mock-runs"
        )
        check.truthy("the mock provider needs no credential", mock_cfg.provider("mock").is_mock)

        os.environ["AF_TEST_KEY"] = "sk-from-env"
        try:
            from_env = load_case(
                root,
                "env-key",
                models_doc(
                    '[provider.p1]\npreset = "openai"\n'
                    'api_key = "env:AF_TEST_KEY"\n',
                    provider_name="p1",
                ),
            )
            check.equal(
                "env: indirection resolves from the environment",
                from_env.provider("p1").api_key,
                "sk-from-env",
            )
        finally:
            del os.environ["AF_TEST_KEY"]

        os.environ.pop("AF_TEST_ABSENT", None)
        absent = error_text(
            lambda: load_case(
                root,
                "env-absent",
                models_doc(
                    '[provider.p1]\npreset = "openai"\n'
                    'api_key = "env:AF_TEST_ABSENT"\n',
                    provider_name="p1",
                ),
            )
        )
        check.contains(
            "an unset environment variable is named in the error",
            absent,
            "AF_TEST_ABSENT",
        )

        # -- roles and cost -------------------------------------------------

        check.section("roles and cost")

        check.equal("every agent role is configured", len(mock_cfg.roles), 7)
        for role in AgentRole:
            check.truthy(
                f"role {role.value} is configured", mock_cfg.role(role) is not None
            )
        check.falsy(
            "the implementer is not forced into JSON",
            mock_cfg.role(AgentRole.IMPLEMENTER).structured,
        )
        check.equal(
            "the mock chain is a single provider",
            provider_names(mock_cfg.provider_chain(AgentRole.QC)),
            ["mock"],
        )

        priced_doc = models_doc(
            '[provider.p1]\npreset = "openai"\napi_key = "sk-test"\n',
            provider_name="p1",
        )
        priced_doc = re.sub(
            r"\[roles\.qc\]\n",
            "[roles.qc]\ncost_per_1k_prompt = 0.5\ncost_per_1k_completion = 1.5\n",
            priced_doc,
            count=1,
        )
        priced = load_case(root, "priced", priced_doc)
        check.equal(
            "cost_for bills prompt and completion separately",
            priced.role(AgentRole.QC).cost_for(2000, 1000),
            2.5,
        )

        negative_doc = re.sub(
            r"\[roles\.qc\]\n",
            "[roles.qc]\ncost_per_1k_prompt = -1.0\n",
            models_doc(
                '[provider.p1]\npreset = "openai"\napi_key = "sk-test"\n',
                provider_name="p1",
            ),
            count=1,
        )
        check.raises(
            "a negative price is refused",
            ConfigError,
            lambda: load_case(root, "negative", negative_doc),
        )

        # -- governor limits -------------------------------------------------

        check.section("governor limits")

        governor = mock_cfg.governor
        check.equal("iteration ceiling", governor.budgets.max_iterations_per_build, 3)
        check.equal("token ceiling", governor.budgets.max_tokens_per_build, 250_000)
        check.equal(
            "wall clock ceiling", governor.budgets.max_wall_clock_seconds, 900.0
        )
        check.same("an unset cost ceiling stays unset", governor.budgets.max_cost_usd, None)
        check.equal(
            "only blockers block",
            list(governor.gates.blocking_severities),
            ["blocker"],
        )
        check.equal("design pass threshold", governor.gates.design_pass_threshold, 7.0)
        check.equal(
            "repeat offender limit", governor.gates.repeat_offender_limit, 2
        )
        check.equal(
            "verbatim iterations kept in context",
            governor.context.recent_iterations_verbatim,
            2,
        )
        check.equal(
            "per-agent context cap", governor.context.per_agent_token_cap, 24_000
        )

        # -- hot reload staging ------------------------------------------------

        check.section("reload staging")

        store = ConfigStore.from_paths(
            models_path=MOCK_MODELS,
            governor_path=GOVERNOR,
            env_path=None,
            runs_dir=root / "store-runs",
        )
        check.falsy("a fresh store has nothing pending", store.has_pending)
        first = store.current
        store.stage_reload()
        check.truthy("stage_reload queues a new config", store.has_pending)
        check.same(
            "staging does not swap the live config yet", store.current, first
        )
        store.apply_pending()
        check.falsy("apply_pending clears the queue", store.has_pending)
        check.check(
            "apply_pending installs a freshly loaded config",
            store.current is not first,
        )

        return check.report()
    finally:
        discard(root)


if __name__ == "__main__":
    passed, total = run()
    raise SystemExit(0 if passed == total else 1)
