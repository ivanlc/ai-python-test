"""Evaluate real Ollama completions and production guardrails independently."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import subprocess
import time
from typing import Any, Dict, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from app.models import Notification
from app.parsing import ExtractionError, extract_notification
from app.provider import SYSTEM_PROMPT


ROOT = Path(__file__).resolve().parents[1]
REFUSAL = re.compile(r"^(?:refused\b|i cannot\b|i can['’]t\b|unable to\b|lo siento\b|no puedo\b)", re.I)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    user_input: str
    expected: Optional[Notification]


def load_cases(path: Path) -> list[Case]:
    cases = [Case.model_validate(item) for item in json.loads(path.read_text())]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation cases must be nonempty and have unique IDs")
    return cases


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _matches(actual: Dict[str, str], expected: Notification) -> bool:
    recipient = actual.get("to", "")
    expected_recipient = expected.to
    if expected.type == "sms":
        recipient = recipient.replace("-", "")
        expected_recipient = expected_recipient.replace("-", "")
    else:
        recipient, expected_recipient = recipient.casefold(), expected_recipient.casefold()
    return (recipient == expected_recipient and actual.get("message") == expected.message
            and actual.get("type") == expected.type)


def score(case: Case, content: str, done_reason: str = "stop") -> Dict[str, Any]:
    """Score exact bodies; never award credit just because fields are present."""
    parsed = None
    try:
        candidate = json.loads(content, object_pairs_hook=_unique_pairs)
        if (isinstance(candidate, dict) and set(candidate) == {"to", "message", "type"}
                and all(isinstance(value, str) for value in candidate.values())):
            parsed = candidate
    except (ValueError, TypeError):
        pass
    refusal = ("{" not in content and "}" not in content
               and bool(REFUSAL.match(content.strip())))
    complete = done_reason == "stop"
    raw_correct = complete and (refusal if case.expected is None
                               else parsed is not None and _matches(parsed, case.expected))
    accepted = None
    guardrail_error = None
    if complete:
        try:
            accepted = extract_notification(content, case.user_input).model_dump()
        except ExtractionError as exc:
            guardrail_error = str(exc)
    else:
        guardrail_error = f"Incomplete model completion: {done_reason}"
    pipeline_correct = (accepted is None if case.expected is None
                        else accepted is not None and _matches(accepted, case.expected))
    return {
        "raw_schema_valid": parsed is not None,
        "raw_refusal": refusal,
        "raw_correct": raw_correct,
        "pipeline_accepted": accepted is not None,
        "accepted_payload": accepted,
        "pipeline_correct": pipeline_correct,
        "valid_intent_rejected": case.expected is not None and accepted is None,
        "guardrail_false_rejection": case.expected is not None and raw_correct and accepted is None,
        "incorrect_acceptance": accepted is not None and not pipeline_correct,
        "model_error_blocked": not raw_correct and accepted is None,
        "guardrail_error": guardrail_error,
    }


def summarize(results: list[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {}
    for arm in sorted({result["arm"] for result in results}):
        items = [result for result in results if result["arm"] == arm]
        scored = [result for result in items if "score" in result]
        durations = sorted(result["latency_seconds"] for result in scored)
        fields = ("raw_correct", "pipeline_correct", "valid_intent_rejected", "guardrail_false_rejection",
                  "incorrect_acceptance", "model_error_blocked")
        summary[arm] = {
            "attempted": len(items),
            "completed": len(scored),
            "execution_errors": len(items) - len(scored),
            **{field: sum(bool(result["score"][field]) for result in scored) for field in fields},
            "median_latency_seconds": round(statistics.median(durations), 3) if durations else None,
            "p95_latency_seconds": round(durations[math.ceil(len(durations) * .95) - 1], 3) if durations else None,
        }
    return summary


def revision() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def guardrail_hash() -> str:
    return hashlib.sha256((ROOT / "app/parsing.py").read_bytes()).hexdigest()


def write_report(path: Path, report: Dict[str, Any]) -> None:
    report["summary"] = summarize(report["results"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


async def run(args) -> int:
    cases = load_cases(args.cases)
    prompts = {"current": SYSTEM_PROMPT}
    if args.include_baseline:
        prompts["baseline"] = (ROOT / "evaluation/baseline-prompt.txt").read_text().strip()
    order = cases.copy()
    random.Random(args.seed).shuffle(order)
    report = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision(),
        "baseline_source_revision": "ad2c1e8" if args.include_baseline else None,
        "guardrail_sha256": guardrail_hash(),
        "dataset_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "model": args.model,
        "endpoint": args.base_url,
        "settings": {"temperature": 0, "seed": args.seed, "num_predict": 512, "num_ctx": 4096,
                     "think": False, "stream": False, "format": None, "concurrency": 1},
        "prompts": {name: {"text": prompt, "sha256": hashlib.sha256(prompt.encode()).hexdigest()}
                    for name, prompt in prompts.items()},
        "cases": [case.model_dump() for case in cases],
        "expected_calls": len(cases) * len(prompts),
        "results": [],
    }
    write_report(args.output, report)
    timeout = httpx.Timeout(connect=5, read=120, write=10, pool=5)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout, trust_env=False) as client:
        for index, case in enumerate(order):
            arms = list(prompts)
            if index % 2:
                arms.reverse()
            for arm in arms:
                result = {"case_id": case.id, "category": case.category, "arm": arm}
                start = time.monotonic()
                try:
                    response = await client.post("/api/chat", json={
                        "model": args.model,
                        "messages": [{"role": "system", "content": prompts[arm]},
                                     {"role": "user", "content": case.user_input}],
                        "stream": False, "think": False, "keep_alive": "5m",
                        "options": {"temperature": 0, "seed": args.seed, "num_predict": 512,
                                    "num_ctx": 4096},
                    })
                    response.raise_for_status()
                    data = response.json()
                    content = data["message"]["content"]
                    if not isinstance(content, str) or data.get("done") is not True:
                        raise ValueError("Incomplete or invalid Ollama response")
                    result.update({
                        "content": content,
                        "done_reason": data.get("done_reason", "unknown"),
                        "latency_seconds": round(time.monotonic() - start, 4),
                        "prompt_tokens": data.get("prompt_eval_count"),
                        "output_tokens": data.get("eval_count"),
                        "score": score(case, content, data.get("done_reason", "unknown")),
                    })
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    result["execution_error"] = f"{type(exc).__name__}: {exc}"
                report["results"].append(result)
                write_report(args.output, report)
                print(f'{len(report["results"])}/{report["expected_calls"]} {arm} {case.id} '
                      f'raw={result.get("score", {}).get("raw_correct")} '
                      f'pipeline={result.get("score", {}).get("pipeline_correct")}', flush=True)
                if "execution_error" in result:
                    report["status"] = "aborted"
                    report["finished_at"] = datetime.now(timezone.utc).isoformat()
                    write_report(args.output, report)
                    return 1
    report["status"] = "complete"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(args.output, report)
    print(json.dumps(report["summary"], indent=2))
    return 0


def rescore(source: Path, output: Path) -> None:
    report = json.loads(source.read_text())
    cases = {item["id"]: Case.model_validate(item) for item in report["cases"]}
    report["rescoring"] = {"source_report": str(source), "source_revision": revision(),
                           "guardrail_sha256": guardrail_hash(),
                           "rescored_at": datetime.now(timezone.utc).isoformat()}
    for result in report["results"]:
        if "content" in result:
            result["score"] = score(cases[result["case_id"]], result["content"], result["done_reason"])
    write_report(output, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--cases", type=Path, default=ROOT / "evaluation/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-baseline", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rescore", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new file to preserve evidence")
    if args.rescore:
        rescore(args.rescore, args.output)
        return 0
    if not args.model:
        parser.error("--model is required for real inference")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
