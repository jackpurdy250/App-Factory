"""App Factory - configuration loading.

Two files, one rule: nothing in core/ or agents/ may name a provider or a
model. Those names exist only in config/models.toml. Credentials exist only in
.env. That separation is what makes a mid-project provider swap a one-line
edit plus !reload instead of a refactor.

Reload semantics: `ConfigStore.stage_reload()` parses and validates the files
immediately (so a typo is reported at once) but holds the result as pending.
`apply_pending()` promotes it, and the pipeline only calls that at a stage
boundary. A config change can therefore never land mid-call.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .schemas.common import AgentRole, Severity

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_FILE = ROOT / "config" / "models.toml"
DEFAULT_GOVERNOR_FILE = ROOT / "config" / "governor.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_RUNS_DIR = ROOT / "runs"
DEFAULT_WEB_DIR = ROOT / "web"
DEFAULT_AGENTS_DIR = ROOT / "agents"

PROVIDER_KINDS = frozenset({"openai_compatible", "anthropic_compatible", "mock"})
NO_CREDENTIAL = "not-required"
PRESETS_FILENAME = "providers.presets.toml"

#: Distinguishes "api_key was never declared" from "api_key is not-required".
_UNSET = object()


class ConfigError(RuntimeError):
    """Raised for any malformed or incomplete configuration.

    Always raised at load time. The pipeline never discovers a config problem
    halfway through a run.
    """


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------


def load_dotenv(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Minimal .env reader. Existing environment variables win, so an export
    in the shell can override the file without editing it."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name:
            continue
        loaded[name] = value
        os.environ.setdefault(name, value)
    return loaded


def resolve_secret(value: str, *, where: str) -> str:
    """Resolve an "env:NAME" reference. Literals pass through unchanged."""
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a non-empty string")
    if not value.startswith("env:"):
        return value
    name = value[4:].strip()
    if not name:
        raise ConfigError(f"{where}: 'env:' reference is missing a variable name")
    resolved = os.environ.get(name)
    if resolved is None or resolved == "":
        raise ConfigError(
            f"{where}: environment variable {name} is unset or empty "
            f"(set it in .env or export it)"
        )
    return resolved


# --------------------------------------------------------------------------
# Typed config
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    kind: str
    base_url: str
    api_key: str

    @property
    def is_mock(self) -> bool:
        return self.kind == "mock"


@dataclass(frozen=True, slots=True)
class RoleConfig:
    role: AgentRole
    provider: str
    model: str
    temperature: float
    max_tokens: int
    structured: bool
    fallback: tuple[str, ...] = ()
    cost_per_1k_prompt: float = 0.0
    cost_per_1k_completion: float = 0.0

    def cost_for(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Dollar cost of one call, from the prices declared in models.toml.

        Both prices default to 0.0, so a config that declares no prices simply
        reports no spend. That is deliberate: the cost ceiling is opt-in, and a
        wrong price is worse than an absent one because it would stop a build
        that had budget left.
        """

        prompt = max(0, prompt_tokens) / 1000.0 * self.cost_per_1k_prompt
        completion = max(0, completion_tokens) / 1000.0 * self.cost_per_1k_completion
        return round(prompt + completion, 6)


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """G2. Three independent ceilings; any one of them escalates."""

    max_iterations_per_build: int = 3
    max_tokens_per_build: int = 250_000
    max_wall_clock_seconds: float = 900.0
    max_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class GateLimits:
    """G3, G5, and the polish ceiling."""

    blocking_severities: frozenset[Severity] = frozenset({Severity.BLOCKER})
    major_fix_attempts: int = 1
    design_pass_threshold: float = 7.0
    repeat_offender_limit: int = 2


@dataclass(frozen=True, slots=True)
class RuleLimits:
    """G7."""

    max_active: int = 5
    default_ttl: int = 3


@dataclass(frozen=True, slots=True)
class ContextLimits:
    recent_iterations_verbatim: int = 2
    per_agent_token_cap: int = 24_000


@dataclass(frozen=True, slots=True)
class GovernorConfig:
    budgets: BudgetLimits = field(default_factory=BudgetLimits)
    gates: GateLimits = field(default_factory=GateLimits)
    rules: RuleLimits = field(default_factory=RuleLimits)
    context: ContextLimits = field(default_factory=ContextLimits)


@dataclass(frozen=True, slots=True)
class AppConfig:
    providers: Mapping[str, ProviderConfig]
    roles: Mapping[AgentRole, RoleConfig]
    governor: GovernorConfig
    models_path: Path
    governor_path: Path
    runs_dir: Path = DEFAULT_RUNS_DIR
    web_dir: Path = DEFAULT_WEB_DIR
    agents_dir: Path = DEFAULT_AGENTS_DIR

    def role(self, role: AgentRole) -> RoleConfig:
        try:
            return self.roles[role]
        except KeyError:
            raise ConfigError(f"no binding configured for role {role.value}") from None

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise ConfigError(f"unknown provider {name!r}") from None

    def provider_chain(self, role: AgentRole) -> tuple[ProviderConfig, ...]:
        """Primary provider first, then each fallback in declared order."""
        binding = self.role(role)
        names = (binding.provider, *binding.fallback)
        return tuple(self.provider(name) for name in names)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: invalid TOML - {exc}") from exc


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return value


def _load_presets(models_path: Path) -> dict[str, dict[str, Any]]:
    """Vendor presets, read from config/providers.presets.toml when present.

    A preset is a saved (kind, base_url) pair with a friendly name, so that
    aiming App Factory at a different AI is a preset name plus an api_key
    rather than a hunt for a base URL. Presets live in config, never in code:
    core/ stays vendor-neutral, which is the same rule that lets models.toml
    own every model string.

    A missing file is not an error. Presets are a convenience; declaring kind
    and base_url directly stays fully supported.
    """
    presets_path = models_path.parent / PRESETS_FILENAME
    if not presets_path.exists():
        return {}

    raw = _read_toml(presets_path)
    table = _require_mapping(raw.get("preset", {}), f"{presets_path.name} [preset]")
    return {
        name: _require_mapping(body, f"{presets_path.name} [preset.{name}]")
        for name, body in table.items()
    }


def _parse_providers(raw: dict[str, Any], path: Path) -> dict[str, ProviderConfig]:
    table = _require_mapping(raw.get("provider", {}), f"{path.name} [provider]")
    if not table:
        raise ConfigError(f"{path.name}: at least one [provider.*] block is required")

    presets = _load_presets(path)
    providers: dict[str, ProviderConfig] = {}
    for name, body in table.items():
        where = f"{path.name} [provider.{name}]"
        block = _require_mapping(body, where)

        # A preset only supplies defaults. Every key declared here wins, so a
        # preset can be borrowed for its wire format and pointed elsewhere.
        defaults: dict[str, Any] = {}
        preset_name = block.get("preset")
        if preset_name is not None:
            if not isinstance(preset_name, str) or not preset_name:
                raise ConfigError(f"{where}: preset must be a non-empty string")
            if preset_name not in presets:
                known = ", ".join(sorted(presets)) or "none defined"
                raise ConfigError(
                    f"{where}: unknown preset {preset_name!r} "
                    f"(known in {PRESETS_FILENAME}: {known})"
                )
            defaults = presets[preset_name]

        kind = block.get("kind", defaults.get("kind"))
        if kind not in PROVIDER_KINDS:
            raise ConfigError(
                f"{where}: set preset = <name from {PRESETS_FILENAME}> or "
                f"kind = one of {sorted(PROVIDER_KINDS)} (got {kind!r})"
            )

        raw_base_url = block.get("base_url", defaults.get("base_url", ""))
        if not isinstance(raw_base_url, str):
            raise ConfigError(f"{where}: base_url must be a string")
        if kind == "mock":
            base_url = ""
        else:
            if not raw_base_url:
                raise ConfigError(f"{where}: base_url is required for kind {kind!r}")
            base_url = resolve_secret(raw_base_url, where=f"{where}.base_url").rstrip("/")

        raw_key = block.get("api_key", defaults.get("api_key", _UNSET))
        if kind == "mock":
            api_key = NO_CREDENTIAL
        elif raw_key is _UNSET:
            # Caught here rather than as a 401 twenty seconds into a build.
            raise ConfigError(
                f"{where}: api_key is required - use \"env:NAME\" to read it from "
                f".env, or \"{NO_CREDENTIAL}\" for a local server that needs none"
            )
        elif raw_key == NO_CREDENTIAL:
            api_key = NO_CREDENTIAL
        else:
            api_key = resolve_secret(raw_key, where=f"{where}.api_key")

        providers[name] = ProviderConfig(
            name=name, kind=kind, base_url=base_url, api_key=api_key
        )
    return providers


def _parse_roles(
    raw: dict[str, Any], providers: Mapping[str, ProviderConfig], path: Path
) -> dict[AgentRole, RoleConfig]:
    table = _require_mapping(raw.get("roles", {}), f"{path.name} [roles]")

    known = {role.value for role in AgentRole}
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"{path.name}: unknown role block(s): {', '.join(unknown)}")

    missing = sorted(known - set(table))
    if missing:
        raise ConfigError(
            f"{path.name}: missing [roles.*] block(s): {', '.join(missing)}"
        )

    roles: dict[AgentRole, RoleConfig] = {}
    for role in AgentRole:
        where = f"{path.name} [roles.{role.value}]"
        block = _require_mapping(table[role.value], where)

        provider_name = block.get("provider")
        if provider_name not in providers:
            raise ConfigError(
                f"{where}: provider {provider_name!r} is not defined "
                f"(known: {', '.join(sorted(providers))})"
            )

        model = block.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{where}: model must be a non-empty string")

        temperature = block.get("temperature", 0.0)
        if not isinstance(temperature, (int, float)) or not 0.0 <= float(temperature) <= 2.0:
            raise ConfigError(f"{where}: temperature must be a number in 0.0-2.0")

        max_tokens = block.get("max_tokens", 2000)
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise ConfigError(f"{where}: max_tokens must be a positive integer")

        structured = block.get("structured", True)
        if not isinstance(structured, bool):
            raise ConfigError(f"{where}: structured must be true or false")
        if role is AgentRole.IMPLEMENTER and structured:
            raise ConfigError(
                f"{where}: structured must be false. The implementer's product is "
                "code on disk, emitted as <file> blocks, not a JSON payload."
            )

        raw_fallback = block.get("fallback", [])
        if not isinstance(raw_fallback, list) or not all(
            isinstance(item, str) for item in raw_fallback
        ):
            raise ConfigError(f"{where}: fallback must be a list of provider names")
        for item in raw_fallback:
            if item not in providers:
                raise ConfigError(f"{where}: fallback provider {item!r} is not defined")
        if provider_name in raw_fallback:
            raise ConfigError(
                f"{where}: fallback must not repeat the primary provider {provider_name!r}"
            )

        prices: dict[str, float] = {}
        for price_key in ("cost_per_1k_prompt", "cost_per_1k_completion"):
            price = block.get(price_key, 0.0)
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or float(price) < 0.0
            ):
                raise ConfigError(f"{where}: {price_key} must be a number >= 0")
            prices[price_key] = float(price)

        roles[role] = RoleConfig(
            role=role,
            provider=provider_name,
            model=model.strip(),
            temperature=float(temperature),
            max_tokens=max_tokens,
            structured=structured,
            fallback=tuple(raw_fallback),
            cost_per_1k_prompt=prices["cost_per_1k_prompt"],
            cost_per_1k_completion=prices["cost_per_1k_completion"],
        )
    return roles


def _parse_governor(raw: dict[str, Any], path: Path) -> GovernorConfig:
    budgets_raw = _require_mapping(raw.get("budgets", {}), f"{path.name} [budgets]")
    gates_raw = _require_mapping(raw.get("gates", {}), f"{path.name} [gates]")
    rules_raw = _require_mapping(raw.get("rules", {}), f"{path.name} [rules]")
    context_raw = _require_mapping(raw.get("context", {}), f"{path.name} [context]")

    def positive_int(table: dict[str, Any], key: str, default: int, where: str) -> int:
        value = table.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{where}.{key}: must be a positive integer")
        return value

    def positive_number(
        table: dict[str, Any], key: str, default: float, where: str
    ) -> float:
        value = table.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{where}.{key}: must be a positive number")
        return float(value)

    max_cost_raw = budgets_raw.get("max_cost_usd", 0)
    if not isinstance(max_cost_raw, (int, float)) or isinstance(max_cost_raw, bool):
        raise ConfigError(f"{path.name} [budgets].max_cost_usd: must be a number")
    max_cost = float(max_cost_raw) if float(max_cost_raw) > 0 else None

    budgets = BudgetLimits(
        max_iterations_per_build=positive_int(
            budgets_raw, "max_iterations_per_build", 3, f"{path.name} [budgets]"
        ),
        max_tokens_per_build=positive_int(
            budgets_raw, "max_tokens_per_build", 250_000, f"{path.name} [budgets]"
        ),
        max_wall_clock_seconds=positive_number(
            budgets_raw, "max_wall_clock_seconds", 900.0, f"{path.name} [budgets]"
        ),
        max_cost_usd=max_cost,
    )

    raw_severities = gates_raw.get("blocking_severities", ["blocker"])
    if not isinstance(raw_severities, list) or not raw_severities:
        raise ConfigError(
            f"{path.name} [gates].blocking_severities: must be a non-empty list"
        )
    severities: set[Severity] = set()
    for item in raw_severities:
        try:
            severities.add(Severity(item))
        except ValueError:
            raise ConfigError(
                f"{path.name} [gates].blocking_severities: {item!r} is not a severity "
                f"(valid: {', '.join(s.value for s in Severity)})"
            ) from None

    threshold = gates_raw.get("design_pass_threshold", 7.0)
    if not isinstance(threshold, (int, float)) or not 0.0 <= float(threshold) <= 10.0:
        raise ConfigError(
            f"{path.name} [gates].design_pass_threshold: must be a number in 0.0-10.0"
        )

    major_attempts = gates_raw.get("major_fix_attempts", 1)
    if not isinstance(major_attempts, int) or isinstance(major_attempts, bool) or major_attempts < 0:
        raise ConfigError(
            f"{path.name} [gates].major_fix_attempts: must be a non-negative integer"
        )

    gates = GateLimits(
        blocking_severities=frozenset(severities),
        major_fix_attempts=major_attempts,
        design_pass_threshold=float(threshold),
        repeat_offender_limit=positive_int(
            gates_raw, "repeat_offender_limit", 2, f"{path.name} [gates]"
        ),
    )

    rules = RuleLimits(
        max_active=positive_int(rules_raw, "max_active", 5, f"{path.name} [rules]"),
        default_ttl=positive_int(rules_raw, "default_ttl", 3, f"{path.name} [rules]"),
    )

    context = ContextLimits(
        recent_iterations_verbatim=positive_int(
            context_raw, "recent_iterations_verbatim", 2, f"{path.name} [context]"
        ),
        per_agent_token_cap=positive_int(
            context_raw, "per_agent_token_cap", 24_000, f"{path.name} [context]"
        ),
    )

    return GovernorConfig(budgets=budgets, gates=gates, rules=rules, context=context)


def load_config(
    models_path: Path | str = DEFAULT_MODELS_FILE,
    governor_path: Path | str = DEFAULT_GOVERNOR_FILE,
    *,
    env_path: Path | str | None = DEFAULT_ENV_FILE,
    runs_dir: Path | str = DEFAULT_RUNS_DIR,
) -> AppConfig:
    """Load and fully validate both config files."""
    if env_path is not None:
        load_dotenv(Path(env_path))

    models_path = Path(models_path)
    governor_path = Path(governor_path)

    models_raw = _read_toml(models_path)
    governor_raw = _read_toml(governor_path)

    providers = _parse_providers(models_raw, models_path)
    roles = _parse_roles(models_raw, providers, models_path)
    governor = _parse_governor(governor_raw, governor_path)

    return AppConfig(
        providers=providers,
        roles=roles,
        governor=governor,
        models_path=models_path,
        governor_path=governor_path,
        runs_dir=Path(runs_dir),
    )


# --------------------------------------------------------------------------
# Reload at stage boundaries
# --------------------------------------------------------------------------


class ConfigStore:
    """Holds the live config and any config staged by !reload.

    Validation happens when the reload is staged, so a typo surfaces
    immediately. Promotion happens only when the pipeline asks, which it does
    between stages.
    """

    def __init__(self, config: AppConfig) -> None:
        self._current = config
        self._pending: AppConfig | None = None

    @classmethod
    def from_paths(
        cls,
        models_path: Path | str = DEFAULT_MODELS_FILE,
        governor_path: Path | str = DEFAULT_GOVERNOR_FILE,
        *,
        env_path: Path | str | None = DEFAULT_ENV_FILE,
        runs_dir: Path | str = DEFAULT_RUNS_DIR,
    ) -> "ConfigStore":
        return cls(
            load_config(
                models_path, governor_path, env_path=env_path, runs_dir=runs_dir
            )
        )

    @property
    def current(self) -> AppConfig:
        return self._current

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def stage_reload(self) -> AppConfig:
        """Re-read and validate both files. Raises ConfigError on any problem,
        leaving the live config untouched."""
        candidate = load_config(
            self._current.models_path,
            self._current.governor_path,
            env_path=None,
            runs_dir=self._current.runs_dir,
        )
        self._pending = candidate
        return candidate

    def apply_pending(self) -> bool:
        """Promote a staged config. Returns True if anything changed."""
        if self._pending is None:
            return False
        self._current = self._pending
        self._pending = None
        return True