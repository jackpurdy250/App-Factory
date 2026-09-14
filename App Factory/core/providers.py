"""App Factory - provider adapters.

Three kinds, selected by `kind` in config/models.toml:

  openai_compatible     POST {base_url}/chat/completions
  anthropic_compatible  POST {base_url}/messages
  mock                  deterministic, offline, no network

HTTP uses the standard library on a worker thread rather than an async HTTP
client, which keeps the dependency list to FastAPI plus Pydantic.

No vendor name appears anywhere else in the codebase. Swapping providers is a
config edit, not a code change.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import NO_CREDENTIAL, ProviderConfig
from .schemas.common import (
    AgentRole,
    IssueCategory,
    Severity,
)

DEFAULT_TIMEOUT = 180.0
ANTHROPIC_VERSION = "2023-06-01"
STRUCTURED_TOOL_NAME = "emit_payload"

#: Statuses that justify trying the next provider in the chain: auth/quota
#: problems and anything server-side. A 400 is our bug and must not be
#: retried against a second provider - it will fail there too.
_RETRYABLE_STATUS = frozenset({401, 402, 408, 409, 425, 429})


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.provider = provider


def is_retryable_status(status: int | None) -> bool:
    if status is None:
        return True  # transport-level failure
    return status in _RETRYABLE_STATUS or 500 <= status <= 599


@dataclass(frozen=True, slots=True)
class ProviderResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str
    latency_ms: int


def _post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    provider: str,
) -> dict[str, Any]:
    """Blocking JSON POST. Called on a worker thread."""
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise ProviderError(
            f"HTTP {exc.code} from {provider}: {detail}",
            status=exc.code,
            retryable=is_retryable_status(exc.code),
            provider=provider,
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(
            f"{provider} unreachable: {exc.reason}",
            status=None,
            retryable=True,
            provider=provider,
        ) from exc
    except TimeoutError as exc:
        raise ProviderError(
            f"{provider} timed out after {timeout:.0f}s",
            status=None,
            retryable=True,
            provider=provider,
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"{provider} returned non-JSON body: {raw[:300]}",
            status=None,
            retryable=False,
            provider=provider,
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderError(
            f"{provider} returned a non-object body",
            status=None,
            retryable=False,
            provider=provider,
        )
    return parsed


class BaseProvider:
    """Adapter interface. One method, no streaming: the pipeline needs whole
    validated payloads, and a partial JSON object is worthless."""

    kind = "base"

    def __init__(self, config: ProviderConfig, *, timeout: float = DEFAULT_TIMEOUT):
        self.config = config
        self.timeout = timeout

    @property
    def name(self) -> str:
        return self.config.name

    async def complete(
        self,
        *,
        role: AgentRole,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> ProviderResult:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {}


class OpenAICompatibleProvider(BaseProvider):
    kind = "openai_compatible"

    def _headers(self) -> dict[str, str]:
        if self.config.api_key == NO_CREDENTIAL:
            return {}
        return {"Authorization": f"Bearer {self.config.api_key}"}

    async def complete(
        self,
        *,
        role: AgentRole,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> ProviderResult:
        body: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_schema is not None:
            # strict=false keeps optional fields legal. Correctness does not
            # depend on the provider honouring this: every reply is validated
            # against the Pydantic model, with one repair attempt.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{role.value}_payload",
                    "strict": False,
                    "schema": json_schema,
                },
            }

        started = time.monotonic()
        data = await asyncio.to_thread(
            _post_json,
            f"{self.config.base_url}/chat/completions",
            self._headers(),
            body,
            self.timeout,
            self.name,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                f"{self.name}: reply contained no choices",
                retryable=False,
                provider=self.name,
            )
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(
                f"{self.name}: reply contained empty content",
                retryable=False,
                provider=self.name,
            )

        usage = data.get("usage") or {}
        return ProviderResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            provider=self.name,
            model=str(data.get("model") or model),
            latency_ms=latency_ms,
        )


class AnthropicCompatibleProvider(BaseProvider):
    kind = "anthropic_compatible"

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self.config.api_key != NO_CREDENTIAL:
            headers["x-api-key"] = self.config.api_key
        return headers

    async def complete(
        self,
        *,
        role: AgentRole,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> ProviderResult:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if json_schema is not None:
            # A forced single-tool call is this API's structured-output
            # mechanism; the tool input IS the payload.
            body["tools"] = [
                {
                    "name": STRUCTURED_TOOL_NAME,
                    "description": f"Emit the {role.value} payload.",
                    "input_schema": json_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}

        started = time.monotonic()
        data = await asyncio.to_thread(
            _post_json,
            f"{self.config.base_url}/messages",
            self._headers(),
            body,
            self.timeout,
            self.name,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise ProviderError(
                f"{self.name}: reply contained no content blocks",
                retryable=False,
                provider=self.name,
            )

        text: str | None = None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                text = json.dumps(block["input"])
                break
        if text is None:
            parts = [
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "".join(parts)
        if not text.strip():
            raise ProviderError(
                f"{self.name}: reply contained empty content",
                retryable=False,
                provider=self.name,
            )

        usage = data.get("usage") or {}
        return ProviderResult(
            text=text,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            provider=self.name,
            model=str(data.get("model") or model),
            latency_ms=latency_ms,
        )


# --------------------------------------------------------------------------
# Mock provider
# --------------------------------------------------------------------------

#: Fixed defect evidence. The wording is constant so that the same defect
#: produces the same fingerprint across iterations, which is what lets the
#: offline run actually exercise G4 and G5.
_BLOCKER_A = ("app.js", IssueCategory.CORRECTNESS, "CSV import crashes when the header row is empty")
_BLOCKER_B = ("app.js", IssueCategory.SPEC_FIDELITY, "monthly summary chart is not rendered at all")
_MAJOR_A = ("index.html", IssueCategory.ACCESSIBILITY, "file input has no associated label element")
_MINOR_A = ("styles.css", IssueCategory.UX_POLISH, "table row height is inconsistent with the header")


class MockProvider(BaseProvider):
    """Deterministic offline provider.

    Not a stub: it returns schema-valid payloads for every role and emits a
    converging defect sequence (2 blockers, then 1, then none) so that a run
    against config/models.mock.toml drives the real gate, the real monotonic
    check, the real rule writer, and a real build on disk.
    """

    kind = "mock"

    async def complete(
        self,
        *,
        role: AgentRole,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> ProviderResult:
        try:
            projection = json.loads(user)
        except json.JSONDecodeError:
            projection = {}
        if not isinstance(projection, dict):
            projection = {}

        iteration = int(projection.get("iteration") or 0)
        task = projection.get("task") or {}
        if not isinstance(task, dict):
            task = {}

        text = self._render(role, iteration, task, projection)
        return ProviderResult(
            text=text,
            prompt_tokens=max(1, len(user) // 4),
            completion_tokens=max(1, len(text) // 4),
            provider=self.name,
            model=model,
            latency_ms=1,
        )

    # -- payload construction ---------------------------------------------

    @staticmethod
    def _issue(
        issue_id: str,
        raised_by: AgentRole,
        severity: Severity,
        spec: tuple[str, IssueCategory, str],
    ) -> dict[str, Any]:
        path, category, evidence = spec
        return {
            "issue_id": issue_id,
            "raised_by": raised_by.value,
            "severity": severity.value,
            "category": category.value,
            "evidence": evidence,
            "file_path": path,
            "suggested_fix": f"Address: {evidence}.",
            "confidence": 0.9,
        }

    def _render(
        self,
        role: AgentRole,
        iteration: int,
        task: dict[str, Any],
        projection: dict[str, Any],
    ) -> str:
        if role is AgentRole.OPTIMIZER:
            raw = str(task.get("raw_input") or "an unspecified feature")
            return json.dumps(
                {
                    "optimized_prompt": (
                        f"Build the following as a single self-contained web artifact: {raw}. "
                        "Use semantic HTML, one stylesheet, and vanilla JavaScript with no "
                        "external dependencies. Persist nothing to a server."
                    ),
                    "inferred_requirements": [
                        "Accept a CSV file chosen by the operator and parse it in the browser.",
                        "Render a per-month summary of the parsed rows.",
                        "Report parse failures visibly instead of failing silently.",
                    ],
                    "ambiguities": [
                        {
                            "ambiguity_id": "AMB-001",
                            "question": "Which CSV column layout should be treated as canonical?",
                            "assumed_answer": "date,description,amount",
                            "resolved": False,
                            "resolved_by_operator": False,
                        }
                    ],
                    "confidence": 0.82,
                }
            )

        if role is AgentRole.OBSERVER and task.get("kind") == "spec":
            inferred = task.get("inferred_requirements") or []
            if not isinstance(inferred, list) or not inferred:
                inferred = ["Deliver the requested feature."]
            requirements = []
            criteria = []
            for index, text in enumerate(inferred[:9], start=1):
                req_id = f"R-{index:03d}"
                ac_id = f"AC-{index:03d}"
                requirements.append(
                    {
                        "req_id": req_id,
                        "text": str(text),
                        "priority": "must" if index == 1 else "should",
                        "acceptance_criteria": [ac_id],
                    }
                )
                criteria.append(
                    {
                        "ac_id": ac_id,
                        "req_id": req_id,
                        "statement": f"Verified when: {text}",
                        "verifiable_by": "qc",
                    }
                )
            return json.dumps(
                {
                    "kind": "spec",
                    "spec": {
                        "spec_version": 1,
                        "goals": ["Ship a single-file browser artifact that works offline."],
                        "requirements": requirements,
                        "constraints": [
                            "No external network calls.",
                            "No build step; the entrypoint must open directly.",
                        ],
                        "out_of_scope": [
                            "Server-side persistence",
                            "Authentication",
                            "Multi-currency handling",
                        ],
                        "acceptance_criteria": criteria,
                    },
                    "scope_verdicts": [],
                    "context_action": "none",
                    "digest": f"Spec frozen at iteration {iteration}.",
                }
            )

        if role is AgentRole.OBSERVER:
            merged = task.get("merged") or []
            return json.dumps(
                {
                    "kind": "adjudication",
                    "merged_issues": merged if isinstance(merged, list) else [],
                    "blocker_fingerprints": [
                        item.get("fingerprint")
                        for item in (merged if isinstance(merged, list) else [])
                        if isinstance(item, dict)
                        and item.get("severity") == Severity.BLOCKER.value
                        and item.get("fingerprint")
                    ],
                    "escalations": [],
                    "precedence_notes": [
                        "Correctness outranks ux_polish; resolved in favour of the QC finding."
                    ],
                    "gate_recommendation": "pass",
                    "scope_verdicts": [
                        {"agent": "qc", "in_scope": True},
                        {"agent": "design", "in_scope": True},
                    ],
                    "context_action": "none",
                }
            )

        if role is AgentRole.IMPLEMENTER:
            return self._implementer_reply(iteration)

        if role is AgentRole.QC:
            if iteration <= 0:
                issues = [
                    self._issue("QC-0001", AgentRole.QC, Severity.BLOCKER, _BLOCKER_A),
                    self._issue("QC-0002", AgentRole.QC, Severity.BLOCKER, _BLOCKER_B),
                    self._issue("QC-0003", AgentRole.QC, Severity.MAJOR, _MAJOR_A),
                ]
            elif iteration == 1:
                issues = [
                    self._issue("QC-0001", AgentRole.QC, Severity.BLOCKER, _BLOCKER_A),
                    self._issue("QC-0003", AgentRole.QC, Severity.MAJOR, _MAJOR_A),
                ]
            else:
                issues = [
                    self._issue("QC-0003", AgentRole.QC, Severity.MAJOR, _MAJOR_A),
                ]
            coverage = [
                req.get("req_id")
                for req in (task.get("requirements") or [])
                if isinstance(req, dict) and req.get("req_id")
            ]
            return json.dumps(
                {
                    "issues": issues,
                    "spec_coverage": coverage,
                    "summary": f"Reviewed iteration {iteration}: {len(issues)} issue(s).",
                    "passed": not any(
                        item["severity"] == Severity.BLOCKER.value for item in issues
                    ),
                }
            )

        if role is AgentRole.DESIGN:
            if iteration <= 0:
                scores = {
                    "hierarchy": 6.0,
                    "density": 6.0,
                    "originality": 5.5,
                    "affordance": 6.5,
                }
            else:
                scores = {
                    "hierarchy": 8.0,
                    "density": 7.5,
                    "originality": 7.0,
                    "affordance": 8.5,
                }
            mean = sum(scores.values()) / 4
            return json.dumps(
                {
                    "issues": [
                        self._issue("DES-0001", AgentRole.DESIGN, Severity.MINOR, _MINOR_A)
                    ],
                    "rubric_scores": scores,
                    "ui_surfaces_reviewed": ["import panel", "summary table"],
                    "summary": f"Rubric mean {mean:.2f} at iteration {iteration}.",
                    "passed": mean >= 7.0,
                }
            )

        if role is AgentRole.PROMPT_ENGINEER:
            blockers = task.get("blockers") or []
            origin = "QC-0001"
            if isinstance(blockers, list) and blockers:
                first = blockers[0]
                if isinstance(first, dict) and first.get("issue_id"):
                    origin = str(first["issue_id"])
            return json.dumps(
                {
                    "analysis": (
                        "The build failed on input validation, not on layout. The prior "
                        "instruction described the happy path only, so degenerate input "
                        "was never considered."
                    ),
                    "rules": [
                        {
                            "rule_text": (
                                "Validate every parsed input before use: guard empty "
                                "headers, missing columns, and non-numeric amounts, and "
                                "surface each failure in the UI."
                            ),
                            "origin_issue": origin,
                            "scope": "implementer",
                            "ttl_iterations": 3,
                        }
                    ],
                }
            )

        return json.dumps({"response": "ack", "detail": None, "reason_code": None})

    @staticmethod
    def _implementer_reply(iteration: int) -> str:
        """A real three-file artifact. Later iterations add the guards and the
        chart the critics asked for, so the defect set genuinely shrinks."""
        guarded = iteration >= 1
        charted = iteration >= 2

        plan = {
            "plan_summary": f"Single-page CSV expense summary, iteration {iteration}.",
            "components": [
                {
                    "name": "import",
                    "responsibility": "Read a CSV file and parse it into rows.",
                    "depends_on": [],
                },
                {
                    "name": "summary",
                    "responsibility": "Aggregate rows by month and render them.",
                    "depends_on": ["import"],
                },
            ],
            "files": [
                {"path": "index.html", "purpose": "Markup and entrypoint", "component": "import"},
                {"path": "styles.css", "purpose": "Presentation", "component": "summary"},
                {"path": "app.js", "purpose": "Parsing and aggregation", "component": "summary"},
            ],
            "dependencies": [],
            "deviations": [],
            "entrypoint": "index.html",
        }

        label = (
            '<label for="csv">Expense CSV</label>'
            if guarded
            else "<span>Expense CSV</span>"
        )
        chart_markup = (
            '    <div id="chart" class="chart" role="img" aria-label="Monthly totals"></div>\n'
            if charted
            else ""
        )
        guard_js = (
            "  if (!header || header.length === 0 || header.every(function (c) { return !c; })) {\n"
            "    throw new Error('CSV header row is empty');\n"
            "  }\n"
            if guarded
            else ""
        )
        chart_js = (
            "function renderChart(totals) {\n"
            "  var host = document.getElementById('chart');\n"
            "  if (!host) { return; }\n"
            "  var max = Math.max.apply(null, totals.map(function (t) { return t.total; }).concat([1]));\n"
            "  host.innerHTML = totals.map(function (t) {\n"
            "    var height = Math.round((t.total / max) * 100);\n"
            "    return '<div class=\"bar\" style=\"height:' + height + '%\"><span>' + t.month + '</span></div>';\n"
            "  }).join('');\n"
            "}\n"
            if charted
            else "function renderChart() { return undefined; }\n"
        )

        html = (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '  <meta charset="utf-8" />\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            "  <title>Expense Summary</title>\n"
            '  <link rel="stylesheet" href="styles.css" />\n'
            "</head>\n"
            "<body>\n"
            "  <main>\n"
            "    <h1>Expense Summary</h1>\n"
            f"    {label}\n"
            '    <input id="csv" type="file" accept=".csv" />\n'
            '    <p id="error" class="error" role="status"></p>\n'
            f"{chart_markup}"
            '    <table id="summary"><thead><tr><th>Month</th><th>Total</th></tr></thead>'
            "<tbody></tbody></table>\n"
            "  </main>\n"
            '  <script src="app.js"></script>\n'
            "</body>\n"
            "</html>"
        )

        css = (
            ":root { --ink: #1c1c1a; --line: #d8d4cc; }\n"
            "body { margin: 0; font: 16px/1.5 system-ui, sans-serif; color: var(--ink); }\n"
            "main { max-width: 46rem; margin: 3rem auto; padding: 0 1.5rem; }\n"
            "h1 { font-size: 1.5rem; letter-spacing: -0.01em; }\n"
            "table { width: 100%; border-collapse: collapse; margin-top: 1.5rem; }\n"
            "th, td { text-align: left; padding: 0.6rem 0; border-bottom: 1px solid var(--line); }\n"
            "td:last-child, th:last-child { text-align: right; font-variant-numeric: tabular-nums; }\n"
            ".error { color: #a3262b; min-height: 1.5rem; }\n"
            ".chart { display: flex; gap: 0.5rem; align-items: flex-end; height: 8rem; margin-top: 1.5rem; }\n"
            ".bar { flex: 1; background: #4a5f4e; position: relative; }\n"
            ".bar span { position: absolute; bottom: -1.4rem; font-size: 0.7rem; width: 100%; text-align: center; }\n"
        )

        js = (
            "'use strict';\n"
            "function parseCsv(text) {\n"
            "  var lines = text.split(/\\r?\\n/).filter(function (l) { return l.trim() !== ''; });\n"
            "  var header = (lines.shift() || '').split(',').map(function (c) { return c.trim(); });\n"
            f"{guard_js}"
            "  return lines.map(function (line) {\n"
            "    var cells = line.split(',');\n"
            "    var row = {};\n"
            "    header.forEach(function (key, i) { row[key] = (cells[i] || '').trim(); });\n"
            "    return row;\n"
            "  });\n"
            "}\n"
            "function summarise(rows) {\n"
            "  var byMonth = {};\n"
            "  rows.forEach(function (row) {\n"
            "    var month = (row.date || '').slice(0, 7);\n"
            "    var amount = parseFloat(row.amount);\n"
            "    if (!month || isNaN(amount)) { return; }\n"
            "    byMonth[month] = (byMonth[month] || 0) + amount;\n"
            "  });\n"
            "  return Object.keys(byMonth).sort().map(function (month) {\n"
            "    return { month: month, total: byMonth[month] };\n"
            "  });\n"
            "}\n"
            f"{chart_js}"
            "function render(totals) {\n"
            "  var body = document.querySelector('#summary tbody');\n"
            "  body.innerHTML = totals.map(function (t) {\n"
            "    return '<tr><td>' + t.month + '</td><td>' + t.total.toFixed(2) + '</td></tr>';\n"
            "  }).join('');\n"
            "  renderChart(totals);\n"
            "}\n"
            "document.getElementById('csv').addEventListener('change', function (event) {\n"
            "  var file = event.target.files && event.target.files[0];\n"
            "  var errorBox = document.getElementById('error');\n"
            "  errorBox.textContent = '';\n"
            "  if (!file) { return; }\n"
            "  var reader = new FileReader();\n"
            "  reader.onload = function () {\n"
            "    try {\n"
            "      render(summarise(parseCsv(String(reader.result))));\n"
            "    } catch (err) {\n"
            "      errorBox.textContent = 'Could not read that file: ' + err.message;\n"
            "    }\n"
            "  };\n"
            "  reader.readAsText(file);\n"
            "});\n"
        )

        return (
            "<plan>\n"
            + json.dumps(plan, indent=2)
            + "\n</plan>\n\n"
            + '<file path="index.html">\n'
            + html
            + "\n</file>\n\n"
            + '<file path="styles.css">\n'
            + css
            + "\n</file>\n\n"
            + '<file path="app.js">\n'
            + js
            + "\n</file>\n\n"
            + "<notes>\n"
            + f"Iteration {iteration}: guards={guarded}, chart={charted}.\n"
            + "</notes>\n"
        )


_PROVIDER_KINDS: dict[str, type[BaseProvider]] = {
    OpenAICompatibleProvider.kind: OpenAICompatibleProvider,
    AnthropicCompatibleProvider.kind: AnthropicCompatibleProvider,
    MockProvider.kind: MockProvider,
}


def build_provider(
    config: ProviderConfig, *, timeout: float = DEFAULT_TIMEOUT
) -> BaseProvider:
    try:
        factory = _PROVIDER_KINDS[config.kind]
    except KeyError:
        raise ProviderError(
            f"unsupported provider kind {config.kind!r}", provider=config.name
        ) from None
    return factory(config, timeout=timeout)