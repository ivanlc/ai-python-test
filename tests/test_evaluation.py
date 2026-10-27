from argparse import Namespace
import json

import httpx
import pytest

from evaluation.run import Case, ROOT, load_cases, rescore, run, score, summarize
from app.models import Notification


def positive_case():
    return Case(id="test", category="email", user_input="Email a luz@example.org: Hola.",
                expected={"to": "luz@example.org", "message": "Hola.", "type": "email"})


def test_evaluator_requires_the_complete_expected_payload():
    case = positive_case()
    correct = score(case, json.dumps(case.expected.model_dump()))
    assert correct["raw_correct"] and correct["pipeline_correct"]
    wrong = score(case, '{"to":"luz@example.org","message":"Hola","type":"email"}')
    assert not wrong["raw_correct"] and not wrong["pipeline_correct"]
    assert wrong["model_error_blocked"]
    assert not wrong["guardrail_false_rejection"]


def test_evaluator_distinguishes_raw_format_from_repair():
    case = positive_case()
    result = score(case, "Output: ```json\n" + json.dumps(case.expected.model_dump()) + "\n```")
    assert not result["raw_correct"]
    assert result["pipeline_correct"]


def test_reject_expected_does_not_make_a_model_error_look_correct():
    case = Case(id="reject", category="missing", user_input="Envía un correo: Hola.", expected=None)
    rejected = score(case, "REFUSED")
    assert rejected["raw_correct"] and rejected["pipeline_correct"]
    invented = score(case, '{"to":"luz@example.org","message":"Hola.","type":"email"}')
    assert not invented["raw_correct"]
    assert invented["pipeline_correct"] and invented["model_error_blocked"]


def test_incorrect_acceptance_is_visible_even_when_output_parses(monkeypatch):
    case = positive_case()
    # Inject a permissive guardrail so the oracle is independent of its correctness.
    monkeypatch.setattr("evaluation.run.extract_notification", lambda *_:
                        Notification(to="luz@example.org", message="hola.", type="email"))
    result = score(case, '{"to":"luz@example.org","message":"hola.","type":"email"}')
    assert result["pipeline_accepted"]
    assert result["incorrect_acceptance"]
    assert not result["pipeline_correct"]


def test_incomplete_completion_cannot_be_scored_as_an_accepted_notification():
    case = positive_case()
    result = score(case, json.dumps(case.expected.model_dump()), done_reason="length")
    assert not result["raw_correct"] and not result["pipeline_accepted"]


def test_summary_keeps_execution_errors_in_the_denominator():
    summary = summarize([
        {"arm": "current", "execution_error": "timeout"},
        {"arm": "current", "latency_seconds": 1.0,
         "score": score(positive_case(), json.dumps(positive_case().expected.model_dump()))},
    ])["current"]
    assert summary["attempted"] == 2
    assert summary["completed"] == summary["raw_correct"] == 1
    assert summary["execution_errors"] == 1


def test_dataset_is_frozen_with_unique_positive_and_negative_cases():
    cases = load_cases(ROOT / "evaluation/cases.json")
    assert len(cases) == 40
    assert sum(case.expected is not None for case in cases) == 24
    assert len({case.id for case in cases}) == len(cases)


def test_holdout_does_not_reuse_main_cases():
    main = load_cases(ROOT / "evaluation/cases.json")
    holdout = load_cases(ROOT / "evaluation/holdout.json")
    assert len(holdout) == 20
    assert sum(case.expected is not None for case in holdout) == 12
    assert not {case.id for case in main} & {case.id for case in holdout}
    assert not {case.user_input for case in main} & {case.user_input for case in holdout}


def run_args(tmp_path):
    cases = [positive_case().model_dump(), {"id": "reject", "category": "missing",
             "user_input": "Manda un correo: Hola.", "expected": None}]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return Namespace(cases=path, include_baseline=False, seed=42, model="test-model",
                     base_url="http://ollama.test", output=tmp_path / "report.json")


async def test_runner_calls_only_the_model_and_preserves_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("evaluation.run.revision", lambda: "test-revision")
    original_client = httpx.AsyncClient
    paths = []

    def handler(request):
        paths.append(request.url.path)
        payload = json.loads(request.content)
        assert [message["role"] for message in payload["messages"]] == ["system", "user"]
        assert payload["think"] is False and payload["stream"] is False
        assert "format" not in payload and "tools" not in payload
        content = (json.dumps(positive_case().expected.model_dump())
                   if "luz@example.org" in payload["messages"][1]["content"] else "REFUSED")
        return httpx.Response(200, json={"message": {"content": content}, "done": True,
                                        "done_reason": "stop"})

    monkeypatch.setattr("evaluation.run.httpx.AsyncClient", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(handler), **kwargs))
    args = run_args(tmp_path)
    assert await run(args) == 0
    report = json.loads(args.output.read_text())
    assert report["status"] == "complete"
    assert len(report["results"]) == report["expected_calls"] == 2
    assert report["summary"]["current"]["pipeline_correct"] == 2
    assert paths == ["/api/chat", "/api/chat"]


async def test_runner_aborts_and_saves_transport_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("evaluation.run.revision", lambda: "test-revision")
    original_client = httpx.AsyncClient

    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    monkeypatch.setattr("evaluation.run.httpx.AsyncClient", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(handler), **kwargs))
    args = run_args(tmp_path)
    assert await run(args) == 1
    report = json.loads(args.output.read_text())
    assert report["status"] == "aborted"
    assert len(report["results"]) == 1
    assert report["summary"]["current"]["execution_errors"] == 1


def test_rescoring_preserves_generation_evidence(tmp_path, monkeypatch):
    case = positive_case()
    original = {"source_revision": "original-generation", "prompts": {"current": "unchanged"},
                "dataset_sha256": "original-dataset", "cases": [case.model_dump()],
                "results": [{"case_id": case.id, "arm": "current", "done_reason": "stop",
                             "content": json.dumps(case.expected.model_dump()), "latency_seconds": 1}]}
    source, target = tmp_path / "original.json", tmp_path / "rescored.json"
    source.write_text(json.dumps(original))
    monkeypatch.setattr("evaluation.run.revision", lambda: "new-validation")
    rescore(source, target)
    updated = json.loads(target.read_text())
    assert updated["results"][0]["content"] == original["results"][0]["content"]
    assert updated["source_revision"] == original["source_revision"]
    assert updated["prompts"] == original["prompts"]
    assert updated["dataset_sha256"] == original["dataset_sha256"]
    assert updated["rescoring"]["source_revision"] == "new-validation"
