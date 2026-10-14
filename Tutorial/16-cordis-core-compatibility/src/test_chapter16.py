from __future__ import annotations

import json
from typing import cast

import pytest
from chapter16 import (
    CORE_SCENARIO_IDS,
    HOST_SCENARIO_IDS,
    REFERENCE_LEDGER,
    REFERENCE_PIN,
    EvidenceKind,
    EvidencePipeline,
    LedgerStatus,
    RecordingSink,
    RunnerRegistry,
    Scenario,
    ScenarioSchemaError,
    UnknownRunnerError,
    Verdict,
    equivalence_claims,
    ledger_counts,
    mismatch_diff,
)

EXPECTED = {"trace": ["activate", "cleanup"], "state": "ACTIVE"}


def core_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "id": "004-core-smoke",
        "purpose": "shared Cordis Core smoke scenario",
        "expected": EXPECTED,
    }
    document.update(overrides)
    return document


def registry_returning(value: object) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("004-core-smoke", lambda _scenario: value)
    return registry


def pinned_metadata() -> dict[str, object]:
    return {"version": REFERENCE_PIN.version, "commit": REFERENCE_PIN.commit}


def test_python_pass_alone_is_not_cross_language_evidence() -> None:
    pipeline = EvidencePipeline(python=registry_returning(EXPECTED))

    comparison = pipeline.evaluate(core_document())

    assert comparison.python.matches_expected
    assert comparison.typescript is None
    assert comparison.verdict is Verdict.PYTHON_ONLY
    assert not comparison.is_equivalence_evidence


def test_paired_pass_on_both_sides_is_cross_language_evidence() -> None:
    pipeline = EvidencePipeline(
        python=registry_returning(EXPECTED),
        typescript=registry_returning(EXPECTED),
        typescript_metadata=pinned_metadata(),
    )

    comparison = pipeline.evaluate(core_document())

    assert comparison.verdict is Verdict.CROSS_LANGUAGE
    assert comparison.is_equivalence_evidence
    assert comparison.python.matches_expected
    assert comparison.typescript is not None
    assert comparison.typescript.matches_expected
    assert comparison.pin_issues == ()


def test_typescript_only_pass_is_not_cross_language_evidence() -> None:
    pipeline = EvidencePipeline(
        python=registry_returning({"trace": ["activate"], "state": "DISPOSED"}),
        typescript=registry_returning(EXPECTED),
        typescript_metadata=pinned_metadata(),
    )

    comparison = pipeline.evaluate(core_document())

    assert comparison.verdict is Verdict.TYPESCRIPT_ONLY
    assert not comparison.is_equivalence_evidence
    assert comparison.python.diff


def test_mismatch_diff_names_every_differing_path() -> None:
    actual = {"trace": ["activate"], "state": "DISPOSED", "extra": 1}

    diff = mismatch_diff(EXPECTED, actual)

    assert '$.trace[1]: missing, expected "cleanup"' in diff
    assert '$.state: expected "ACTIVE", got "DISPOSED"' in diff
    assert "$.extra: unexpected 1" in diff
    assert mismatch_diff(EXPECTED, EXPECTED) == ()


def test_typescript_evidence_requires_the_pinned_version_and_commit() -> None:
    calls = 0

    def typescript_runner(_scenario: Scenario) -> object:
        nonlocal calls
        calls += 1
        return EXPECTED

    typescript = RunnerRegistry()
    typescript.register("004-core-smoke", typescript_runner)

    wrong_version = EvidencePipeline(
        python=registry_returning(EXPECTED),
        typescript=typescript,
        typescript_metadata={"version": "4.0.1", "commit": REFERENCE_PIN.commit},
    ).evaluate(core_document())
    missing_commit = EvidencePipeline(
        python=registry_returning(EXPECTED),
        typescript=typescript,
        typescript_metadata={"version": REFERENCE_PIN.version},
    ).evaluate(core_document())

    assert calls == 0  # an unpinned build is never executed as evidence
    assert wrong_version.verdict is Verdict.PYTHON_ONLY
    assert any("4.0.1" in issue for issue in wrong_version.pin_issues)
    assert missing_commit.typescript is not None
    assert missing_commit.typescript.available is False
    assert any("'commit'" in issue for issue in missing_commit.pin_issues)
    assert missing_commit.verdict is Verdict.PYTHON_ONLY


def test_failure_output_is_normalized_deterministically() -> None:
    registry = RunnerRegistry()

    def failing(_scenario: Scenario) -> object:
        raise ValueError("boom")

    registry.register("004-core-smoke", failing)
    pipeline = EvidencePipeline(python=registry)

    comparison = pipeline.evaluate(core_document())
    report = json.dumps(comparison.report(), ensure_ascii=False, sort_keys=True)

    assert comparison.python.available is True
    assert comparison.python.normalized == {"error": "ValueError", "message": "boom"}
    assert comparison.python.note == "runner raised; failure output normalized"
    assert comparison.verdict is Verdict.NEITHER
    assert "Traceback" not in report
    assert '"error": "ValueError"' in report


def test_scenario_schema_rejects_core_scenario_without_expected() -> None:
    with pytest.raises(ScenarioSchemaError, match=r"\$\.expected"):
        Scenario.from_document({"id": "004-core-smoke"})

    with pytest.raises(ScenarioSchemaError, match=r"\$\.id"):
        Scenario.from_document({"id": ""})

    scenario = Scenario.from_document(core_document())
    assert scenario.kind is EvidenceKind.CORE
    assert scenario.expected == EXPECTED


def test_host_scenarios_are_not_counted_as_python_failures() -> None:
    counts = ledger_counts()

    assert len(REFERENCE_LEDGER) == 24
    assert len(CORE_SCENARIO_IDS) == 21
    assert len(HOST_SCENARIO_IDS) == 3
    assert counts["python:pass"] == 21
    assert counts["python:not-applicable"] == 3
    assert counts["typescript:not-run"] == 24
    assert equivalence_claims() == ()
    assert all(
        record.python_status is LedgerStatus.PASS
        for record in REFERENCE_LEDGER
        if record.kind is EvidenceKind.CORE
    )

    host_document = {
        "id": "001-provider-replacement",
        "purpose": "A consumer unloads when its provider disappears.",
        "steps": [{"action": "mount", "plugin": "consumer"}],
    }
    comparison = EvidencePipeline(
        python=registry_returning(EXPECTED),
        typescript=registry_returning(EXPECTED),
        typescript_metadata=pinned_metadata(),
    ).evaluate(host_document)

    assert comparison.verdict is Verdict.NOT_APPLICABLE
    assert comparison.typescript is None
    assert not comparison.is_equivalence_evidence


def test_runner_registry_reports_missing_runner_and_lists_ids() -> None:
    registry = RunnerRegistry()
    registry.register("004-core-smoke", lambda _scenario: EXPECTED)

    assert "004-core-smoke" in registry
    assert registry.ids() == ("004-core-smoke",)
    assert len(registry) == 1
    with pytest.raises(UnknownRunnerError, match="005-plugin-shapes"):
        registry.run(
            Scenario.from_document({"id": "005-plugin-shapes", "expected": {}})
        )

    comparison = EvidencePipeline(python=registry).evaluate(
        {"id": "005-plugin-shapes", "expected": {}}
    )

    assert comparison.python.available is False
    assert "no runner registered" in comparison.python.note
    assert comparison.verdict is Verdict.NEITHER


def test_report_is_written_to_the_injected_sink() -> None:
    sink = RecordingSink()
    pipeline = EvidencePipeline(
        python=registry_returning(EXPECTED),
        typescript=registry_returning(EXPECTED),
        typescript_metadata=pinned_metadata(),
        sink=sink,
    )

    pipeline.evaluate(core_document())

    assert len(pipeline.runs) == 1
    assert len(sink.lines) == 1
    report = cast(dict[str, object], json.loads(sink.lines[0]))
    assert report["scenario"] == "004-core-smoke"
    assert report["kind"] == "core-oracle"
    assert report["verdict"] == "cross-language"
    assert report["pin_issues"] == []
    python_report = cast(dict[str, object], report["python"])
    assert python_report["matches_expected"] is True
    assert python_report["diff"] == []
