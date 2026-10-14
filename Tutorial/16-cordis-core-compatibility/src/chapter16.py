"""Cross-language evidence pipeline for Cordis Core compatibility (chapter 16).

This chapter does not implement a second Cordis and does not re-run one.  It
implements the machinery that decides whether a scenario result *counts* as
cross-language evidence:

* a scenario schema that separates Cordis Core oracle scenarios from
  Harness-shaped host scenarios;
* deterministic normalization of results and failures;
* one injectable runner registry per language;
* an expected check, a paired comparison and an explicit mismatch diff.

The runtime facts recorded here are a snapshot of ``cordis-py/tests/compat``:
the Python oracle passes the 21 Cordis Core scenarios ``004``-``024``, the pinned
TypeScript 4.0.2 build has not been run in this checkout, and ``001``-``003`` are
Harness-shaped host scenarios that ``oracle.py`` does not implement.  A pipeline
run in this module never upgrades those facts into an equivalence claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

Scope = str


class ScenarioSchemaError(ValueError):
    """Raised when a scenario document does not match the shared schema."""

    def __init__(self, message: str, path: Scope = "$") -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


class UnknownRunnerError(LookupError):
    """Raised when a language has no runner registered for a scenario id."""


class EvidenceKind(StrEnum):
    """What kind of evidence a scenario can produce."""

    CORE = "core-oracle"
    HOST = "host-shaped"


class Verdict(StrEnum):
    """What a paired comparison actually establishes."""

    CROSS_LANGUAGE = "cross-language"
    PYTHON_ONLY = "python-only"
    TYPESCRIPT_ONLY = "typescript-only"
    NEITHER = "neither"
    NOT_APPLICABLE = "not-applicable"


class LedgerStatus(StrEnum):
    """Recorded run status of one language side of one scenario."""

    PASS = "pass"
    NOT_APPLICABLE = "not-applicable"
    NOT_RUN = "not-run"


# --------------------------------------------------------------------------- #
# Reference pin
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReferencePin:
    """The exact reference build that TypeScript evidence must come from."""

    version: str
    commit: str
    entry_env: str

    def issues(self, metadata: Mapping[str, object] | None) -> tuple[str, ...]:
        """Return one line per missing or mismatching pin field."""

        if metadata is None:
            return (
                "reference metadata is missing: 'version' and 'commit' are required",
            )
        issues: list[str] = []
        version = metadata.get("version")
        if version is None:
            issues.append("reference metadata is missing 'version'")
        elif version != self.version:
            issues.append(
                f"reference version {version!r} does not match pinned {self.version!r}"
            )
        commit = metadata.get("commit")
        if commit is None:
            issues.append("reference metadata is missing 'commit'")
        elif commit != self.commit:
            issues.append(
                f"reference commit {commit!r} does not match pinned {self.commit!r}"
            )
        return tuple(issues)


REFERENCE_PIN = ReferencePin(
    version="4.0.2",
    commit="dd6322d604e00eec1ba5e0c8541159906a21094a",
    entry_env="CORDIS_REFERENCE_ENTRY",
)


# --------------------------------------------------------------------------- #
# Scenario schema
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Scenario:
    """One shared scenario after schema validation.

    Core oracle scenarios must declare ``expected`` as one comparable result
    object.  Harness-shaped host scenarios describe an external system instead:
    ``001`` declares per-step outcome records, ``002`` declares host artifacts
    and ``003`` declares only an ordered state list, so none of them is a
    normalized result this pipeline can compare.  Their classified ``kind`` is
    what excludes them, not the presence or absence of a key.
    """

    id: str
    kind: EvidenceKind
    expected: object
    purpose: str = ""
    typescript_evidence: tuple[str, ...] = ()

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Scenario:
        """Validate one scenario document and classify it."""

        if not isinstance(document, Mapping):
            raise ScenarioSchemaError("scenario document must be an object")
        scenario_id = document.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ScenarioSchemaError("'id' must be a non-empty string", "$.id")
        purpose = document.get("purpose", "")
        if not isinstance(purpose, str):
            raise ScenarioSchemaError("'purpose' must be a string", "$.purpose")
        evidence = document.get("typescript_evidence", [])
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise ScenarioSchemaError(
                "'typescript_evidence' must be an array of strings",
                "$.typescript_evidence",
            )
        kind = (
            EvidenceKind.HOST if scenario_id in HOST_SCENARIO_IDS else EvidenceKind.CORE
        )
        if kind is EvidenceKind.CORE and "expected" not in document:
            raise ScenarioSchemaError(
                "core oracle scenario requires 'expected'", "$.expected"
            )
        expected = document.get("expected") if kind is EvidenceKind.CORE else None
        return cls(
            id=scenario_id,
            kind=kind,
            expected=expected,
            purpose=purpose,
            typescript_evidence=tuple(evidence),
        )


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def normalize(value: object) -> object:
    """Return the canonical JSON-comparable form of one result value.

    The form is deliberately small: mappings become string-keyed dictionaries,
    sequences become lists, enums become their string value, paths become
    strings, and everything else must already be a JSON scalar.  Objects with no
    portable representation become a deterministic type marker instead of a
    memory address or a ``repr`` that changes between runs.
    """

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        mapping = value
        return {str(key): normalize(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        return [normalize(item) for item in value]
    if isinstance(value, BaseException):
        return normalize_failure(value)
    return {"unrepresentable": type(value).__qualname__}


def normalize_failure(error: BaseException) -> dict[str, str]:
    """Normalize a raised exception so failure output is stable evidence."""

    return {"error": type(error).__name__, "message": str(error)}


def _compact(value: object) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True)


def mismatch_diff(
    expected: object,
    actual: object,
    path: Scope = "$",
) -> tuple[str, ...]:
    """Return one line per structural difference between expected and actual."""

    lines: list[str] = []
    _diff(normalize(expected), normalize(actual), path, lines)
    return tuple(lines)


def _diff(expected: object, actual: object, path: Scope, lines: list[str]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted({*expected, *actual}):
            child = f"{path}.{key}"
            if key not in actual:
                lines.append(f"{child}: missing, expected {_compact(expected[key])}")
            elif key not in expected:
                lines.append(f"{child}: unexpected {_compact(actual[key])}")
            else:
                _diff(expected[key], actual[key], child, lines)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            child = f"{path}[{index}]"
            if index >= len(actual):
                lines.append(f"{child}: missing, expected {_compact(expected[index])}")
            elif index >= len(expected):
                lines.append(f"{child}: unexpected {_compact(actual[index])}")
            else:
                _diff(expected[index], actual[index], child, lines)
        return
    if expected != actual:
        lines.append(f"{path}: expected {_compact(expected)}, got {_compact(actual)}")


# --------------------------------------------------------------------------- #
# Runners and output sinks
# --------------------------------------------------------------------------- #

Runner = Callable[[Scenario], object]


class RunnerRegistry:
    """One language's scenario runners, injectable per test or per real run."""

    def __init__(self) -> None:
        self._runners: dict[str, Runner] = {}

    def register(self, scenario_id: str, runner: Runner) -> Runner:
        """Register a runner and return it, so this can also be a decorator."""

        self._runners[scenario_id] = runner
        return runner

    def run(self, scenario: Scenario) -> object:
        """Execute the registered runner or report that this side is absent."""

        runner = self._runners.get(scenario.id)
        if runner is None:
            raise UnknownRunnerError(f"no runner registered for {scenario.id!r}")
        return runner(scenario)

    def __contains__(self, scenario_id: object) -> bool:
        return scenario_id in self._runners

    def __len__(self) -> int:
        return len(self._runners)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))


class OutputSink(Protocol):
    """Where a pipeline writes its normalized report."""

    def write(self, text: str) -> None: ...


class NullSink:
    """Sink that discards reports; the default for programmatic use."""

    def write(self, text: str) -> None:
        return None


class RecordingSink:
    """Sink that keeps every written report for assertions or CLI output."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)


# --------------------------------------------------------------------------- #
# Paired comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SideResult:
    """One language's normalized result for one scenario."""

    name: str
    available: bool
    normalized: object | None
    matches_expected: bool
    diff: tuple[str, ...]
    note: str = ""

    def report(self) -> dict[str, object]:
        return {
            "available": self.available,
            "matches_expected": self.matches_expected,
            "normalized": self.normalized,
            "diff": list(self.diff),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """The full paired verdict for one scenario."""

    scenario_id: str
    kind: EvidenceKind
    verdict: Verdict
    python: SideResult
    typescript: SideResult | None
    pin_issues: tuple[str, ...]

    @property
    def is_equivalence_evidence(self) -> bool:
        return self.verdict is Verdict.CROSS_LANGUAGE

    def report(self) -> dict[str, object]:
        return {
            "scenario": self.scenario_id,
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "python": self.python.report(),
            "typescript": None if self.typescript is None else self.typescript.report(),
            "pin_issues": list(self.pin_issues),
        }


def classify(
    scenario: Scenario,
    python: SideResult,
    typescript: SideResult | None,
) -> Verdict:
    """Decide what a paired result establishes; never inflate one side."""

    if scenario.kind is EvidenceKind.HOST:
        return Verdict.NOT_APPLICABLE
    if typescript is None or not typescript.available:
        return Verdict.PYTHON_ONLY if python.matches_expected else Verdict.NEITHER
    if python.matches_expected and typescript.matches_expected:
        return Verdict.CROSS_LANGUAGE
    if python.matches_expected:
        return Verdict.PYTHON_ONLY
    if typescript.matches_expected:
        return Verdict.TYPESCRIPT_ONLY
    return Verdict.NEITHER


class EvidencePipeline:
    """Run one scenario on both sides and report a normalized comparison."""

    def __init__(
        self,
        *,
        python: RunnerRegistry,
        typescript: RunnerRegistry | None = None,
        typescript_metadata: Mapping[str, object] | None = None,
        pin: ReferencePin = REFERENCE_PIN,
        sink: OutputSink | None = None,
    ) -> None:
        self.python = python
        self.typescript = typescript
        self.typescript_metadata = typescript_metadata
        self.pin = pin
        self.sink = sink if sink is not None else NullSink()
        self.runs: list[Comparison] = []

    def evaluate(self, document: Mapping[str, object]) -> Comparison:
        """Validate, run both sides, compare with ``expected`` and report."""

        scenario = Scenario.from_document(document)
        if scenario.kind is EvidenceKind.HOST:
            comparison = Comparison(
                scenario_id=scenario.id,
                kind=scenario.kind,
                verdict=Verdict.NOT_APPLICABLE,
                python=SideResult(
                    name="python",
                    available=False,
                    normalized=None,
                    matches_expected=False,
                    diff=(),
                    note="host-shaped scenario: the Cordis Core oracle does not implement it",
                ),
                typescript=None,
                pin_issues=(),
            )
            return self._emit(comparison)

        python_side = self._evaluate_side("python", self.python, scenario)
        typescript_side: SideResult | None = None
        pin_issues: tuple[str, ...] = ()
        if self.typescript is not None:
            pin_issues = self.pin.issues(self.typescript_metadata)
            if pin_issues:
                typescript_side = SideResult(
                    name="typescript",
                    available=False,
                    normalized=None,
                    matches_expected=False,
                    diff=(),
                    note="; ".join(pin_issues),
                )
            else:
                typescript_side = self._evaluate_side(
                    "typescript", self.typescript, scenario
                )
        comparison = Comparison(
            scenario_id=scenario.id,
            kind=scenario.kind,
            verdict=classify(scenario, python_side, typescript_side),
            python=python_side,
            typescript=typescript_side,
            pin_issues=pin_issues,
        )
        return self._emit(comparison)

    def _evaluate_side(
        self,
        name: str,
        registry: RunnerRegistry,
        scenario: Scenario,
    ) -> SideResult:
        try:
            raw = registry.run(scenario)
        except UnknownRunnerError as error:
            return SideResult(
                name=name,
                available=False,
                normalized=None,
                matches_expected=False,
                diff=(),
                note=str(error),
            )
        except BaseException as error:  # noqa: BLE001 - a failed run is evidence, not a pipeline crash
            normalized = normalize_failure(error)
            return SideResult(
                name=name,
                available=True,
                normalized=normalized,
                matches_expected=False,
                diff=mismatch_diff(scenario.expected, normalized),
                note="runner raised; failure output normalized",
            )
        normalized = normalize(raw)
        diff = mismatch_diff(scenario.expected, normalized)
        return SideResult(
            name=name,
            available=True,
            normalized=normalized,
            matches_expected=not diff,
            diff=diff,
        )

    def _emit(self, comparison: Comparison) -> Comparison:
        self.runs.append(comparison)
        self.sink.write(
            json.dumps(comparison.report(), ensure_ascii=False, sort_keys=True)
        )
        return comparison


# --------------------------------------------------------------------------- #
# Recorded evidence ledger
# --------------------------------------------------------------------------- #

HOST_SCENARIO_IDS: tuple[str, ...] = (
    "001-provider-replacement",
    "002-harness-adapter-reload",
    "003-harness-llm-service-loss",
)

CORE_SCENARIO_IDS: tuple[str, ...] = (
    "004-core-smoke",
    "005-plugin-shapes",
    "006-context-registry",
    "007-inject-delete",
    "008-strict-get",
    "009-inject-metadata",
    "010-context-filter",
    "011-inject-config",
    "012-event-contracts",
    "013-effect-contracts",
    "014-fiber-contracts",
    "015-fiber-invalid-update",
    "016-fiber-failures",
    "017-fiber-dependency-races",
    "018-reflect-service",
    "019-accessor-mixin",
    "020-logger-contracts",
    "021-logger-options",
    "022-disposable-list",
    "023-service-facades",
    "024-remaining-contracts",
)


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """Recorded status of one scenario's two language sides."""

    scenario: str
    kind: EvidenceKind
    python_runner: bool
    python_status: LedgerStatus
    typescript_status: LedgerStatus


REFERENCE_LEDGER: tuple[LedgerRecord, ...] = (
    *(
        LedgerRecord(
            scenario=scenario_id,
            kind=EvidenceKind.HOST,
            python_runner=False,
            python_status=LedgerStatus.NOT_APPLICABLE,
            typescript_status=LedgerStatus.NOT_RUN,
        )
        for scenario_id in HOST_SCENARIO_IDS
    ),
    *(
        LedgerRecord(
            scenario=scenario_id,
            kind=EvidenceKind.CORE,
            python_runner=True,
            python_status=LedgerStatus.PASS,
            typescript_status=LedgerStatus.NOT_RUN,
        )
        for scenario_id in CORE_SCENARIO_IDS
    ),
)


def ledger_counts(
    ledger: tuple[LedgerRecord, ...] = REFERENCE_LEDGER,
) -> dict[str, int]:
    """Count records per status; used by the chapter tests and the README."""

    counts: dict[str, int] = {}
    for record in ledger:
        counts[f"python:{record.python_status.value}"] = (
            counts.get(f"python:{record.python_status.value}", 0) + 1
        )
        counts[f"typescript:{record.typescript_status.value}"] = (
            counts.get(f"typescript:{record.typescript_status.value}", 0) + 1
        )
    return counts


def equivalence_claims(
    ledger: tuple[LedgerRecord, ...] = REFERENCE_LEDGER,
) -> tuple[str, ...]:
    """Scenarios whose recorded evidence actually supports an equivalence claim."""

    return tuple(
        record.scenario
        for record in ledger
        if record.python_status is LedgerStatus.PASS
        and record.typescript_status is LedgerStatus.PASS
    )


__all__ = [
    "CORE_SCENARIO_IDS",
    "HOST_SCENARIO_IDS",
    "REFERENCE_LEDGER",
    "REFERENCE_PIN",
    "Comparison",
    "EvidenceKind",
    "EvidencePipeline",
    "LedgerRecord",
    "LedgerStatus",
    "NullSink",
    "OutputSink",
    "RecordingSink",
    "ReferencePin",
    "Runner",
    "RunnerRegistry",
    "Scenario",
    "ScenarioSchemaError",
    "SideResult",
    "UnknownRunnerError",
    "Verdict",
    "classify",
    "equivalence_claims",
    "ledger_counts",
    "mismatch_diff",
    "normalize",
    "normalize_failure",
]
