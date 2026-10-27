"""Evaluate real model completions (Ollama or Anthropic) and production guardrails independently."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
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
from app.guardrails import ExtractionError, ModelDeclined, RequestRejected, check_request, extract_notification
from app.ai.prompts import SYSTEM_PROMPT, retry_instructions


ROOT = Path(__file__).resolve().parents[1]
OLLAMA_URL = "http://127.0.0.1:11434"
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
    declined = False
    try:
        # The service rejects these before calling the model (pre-flight).
        check_request(case.user_input)
        request_rejected = False
    except RequestRejected:
        request_rejected = True
    if complete:
        try:
            accepted = extract_notification(content, case.user_input).model_dump()
        except ExtractionError as exc:
            guardrail_error = str(exc)
            declined = isinstance(exc, ModelDeclined)
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
        "request_rejected": request_rejected,
        "model_declined": declined,
    }


def summarize(results: list[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {}
    for arm in sorted({result["arm"] for result in results}):
        items = [result for result in results if result["arm"] == arm]
        scored = [result for result in items if "score" in result]
        durations = sorted(result["latency_seconds"] for result in scored)
        fields = ("raw_correct", "pipeline_correct", "valid_intent_rejected", "guardrail_false_rejection",
                  "incorrect_acceptance", "model_error_blocked")
        reasked = [result for result in scored if "reask" in result]
        reask_stats = {}
        if reasked or any("reask_enabled" in result for result in scored):
            final = [result["reask"]["score"] if "reask" in result else result["score"] for result in scored]
            reask_stats = {
                "reask_calls": len(reasked),
                "pipeline_correct_with_reask": sum(bool(item["pipeline_correct"]) for item in final),
                "incorrect_acceptance_with_reask": sum(bool(item["incorrect_acceptance"]) for item in final),
            }
        summary[arm] = {
            "attempted": len(items),
            "completed": len(scored),
            "execution_errors": len(items) - len(scored),
            **{field: sum(bool(result["score"][field]) for result in scored) for field in fields},
            "median_latency_seconds": round(statistics.median(durations), 3) if durations else None,
            "p95_latency_seconds": round(durations[math.ceil(len(durations) * .95) - 1], 3) if durations else None,
            **reask_stats,
        }
    return summary


def revision() -> str:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "app", "evaluation/run.py"],
                                    cwd=ROOT, text=True).strip()
    return f"{head}-dirty" if dirty else head


def guardrail_hash() -> str:
    """SHA-256 over every guardrail module (path and content), in a stable order."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "app/guardrails").glob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def write_report(path: Path, report: Dict[str, Any]) -> None:
    report["summary"] = summarize(report["results"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


ANTHROPIC_URL = "https://api.anthropic.com"


async def _complete_ollama(client: httpx.AsyncClient, args, system: str, user: str) -> Dict[str, Any]:
    response = await client.post("/api/chat", json={
        "model": args.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False, "keep_alive": "5m",
        "options": {"temperature": 0, "seed": args.seed, "num_predict": 512, "num_ctx": 4096},
    })
    response.raise_for_status()
    data = response.json()
    content = data["message"]["content"]
    if not isinstance(content, str) or data.get("done") is not True:
        raise ValueError("Incomplete or invalid Ollama response")
    return {"content": content, "done_reason": data.get("done_reason", "unknown"),
            "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}


async def _complete_anthropic(client: httpx.AsyncClient, args, system: str, user: str) -> Dict[str, Any]:
    # Same system/user separation as the challenge contract; no tools, prefill or JSON mode.
    for attempt in range(4):
        response = await client.post("/v1/messages", json={
            "model": args.model, "max_tokens": 512, "system": system,
            # Mirrors `think: false` for Ollama. With adaptive thinking enabled this
            # model occasionally returned only a thinking block and no text.
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": user}],
        })
        if response.status_code in (429, 500, 502, 503, 529) and attempt < 3:
            await asyncio.sleep(float(response.headers.get("retry-after", 2 * 2 ** attempt)))
            continue
        break
    response.raise_for_status()
    data = response.json()
    texts = [block["text"] for block in data["content"] if block.get("type") == "text"]
    if len(texts) > 1:
        raise ValueError("Expected at most one text block")
    stop = data.get("stop_reason")
    usage = data.get("usage", {})
    # An empty answer is a model failure to score, not a transport failure.
    return {"content": texts[0] if texts else "",
            "done_reason": "stop" if stop in ("end_turn", "stop_sequence") else str(stop),
            "prompt_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def _client(args) -> httpx.AsyncClient:
    timeout = httpx.Timeout(connect=5, read=120, write=10, pool=5)
    if getattr(args, "backend", "ollama") == "anthropic":
        key = os.environ.get(args.api_key_env, "")
        if not key:
            raise SystemExit(f"Set {args.api_key_env} to use the Anthropic backend")
        return httpx.AsyncClient(base_url=args.base_url or ANTHROPIC_URL, timeout=timeout,
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return httpx.AsyncClient(base_url=args.base_url or OLLAMA_URL, timeout=timeout, trust_env=False)


def _settings(args) -> Dict[str, Any]:
    if getattr(args, "backend", "ollama") == "anthropic":
        # This model family rejects `temperature`; repeats expose its variance instead.
        return {"backend": "anthropic", "temperature": None, "max_tokens": 512, "thinking": "disabled",
                "reask": getattr(args, "reask", False),
                "repeats": getattr(args, "repeats", 1), "concurrency": 1}
    return {"backend": "ollama", "temperature": 0, "seed": args.seed, "num_predict": 512,
            "num_ctx": 4096, "think": False, "stream": False, "format": None,
            "reask": getattr(args, "reask", False),
            "repeats": getattr(args, "repeats", 1), "concurrency": 1}


def load_prompts(args) -> Dict[str, str]:
    prompts = {"current": SYSTEM_PROMPT}
    if args.include_baseline:
        prompts["baseline"] = (ROOT / "evaluation/baseline-prompt.txt").read_text().strip()
    for spec in getattr(args, "prompt", None) or []:
        name, _, path = spec.partition("=")
        if not name or not path or name in prompts:
            raise SystemExit(f"Invalid or duplicate --prompt {spec!r}; use NAME=PATH")
        prompts[name] = Path(path).read_text().strip()
    return prompts


async def run(args) -> int:
    cases = load_cases(args.cases)
    prompts = load_prompts(args)
    repeats = getattr(args, "repeats", 1)
    backend = getattr(args, "backend", "ollama")
    complete = _complete_anthropic if backend == "anthropic" else _complete_ollama
    order = cases.copy()
    random.Random(args.seed).shuffle(order)
    report = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision(),
        "baseline_source_revision": "ad2c1e8" if args.include_baseline else None,
        "guardrail_sha256": guardrail_hash(),
        "dataset": str(args.cases.relative_to(ROOT) if args.cases.is_relative_to(ROOT) else args.cases),
        "dataset_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "model": args.model,
        "endpoint": args.base_url or (ANTHROPIC_URL if backend == "anthropic" else OLLAMA_URL),
        "settings": _settings(args),
        "prompts": {name: {"text": prompt, "sha256": hashlib.sha256(prompt.encode()).hexdigest()}
                    for name, prompt in prompts.items()},
        "cases": [case.model_dump() for case in cases],
        "expected_calls": len(cases) * len(prompts) * repeats,
        "results": [],
    }
    write_report(args.output, report)
    async with _client(args) as client:
        for repeat in range(repeats):
            for index, case in enumerate(order):
                arms = list(prompts)
                # Rotate arm order so no prompt is always first.
                shift = (index + repeat) % len(arms)
                arms = arms[shift:] + arms[:shift]
                for arm in arms:
                    result: Dict[str, Any] = {"case_id": case.id, "category": case.category, "arm": arm}
                    if repeats > 1:
                        result["repeat"] = repeat
                    start = time.monotonic()
                    try:
                        completion = await complete(client, args, prompts[arm], case.user_input)
                        result.update({
                            "content": completion["content"],
                            "done_reason": completion["done_reason"],
                            "latency_seconds": round(time.monotonic() - start, 4),
                            "prompt_tokens": completion["prompt_tokens"],
                            "output_tokens": completion["output_tokens"],
                            "score": score(case, completion["content"], completion["done_reason"]),
                        })
                        first = result["score"]
                        if getattr(args, "reask", False):
                            result["reask_enabled"] = True
                        # Mirror the service: one re-ask with the fixed reason, never
                        # after a request-level rejection or an accepted output.
                        if (getattr(args, "reask", False) and not first["pipeline_accepted"]
                                and not first["request_rejected"] and not first["model_declined"]
                                and completion["done_reason"] == "stop"):
                            feedback = first["guardrail_error"]
                            retry = await complete(client, args, prompts[arm] + retry_instructions(feedback),
                                                   case.user_input)
                            result["reask"] = {"feedback": feedback, "content": retry["content"],
                                               "done_reason": retry["done_reason"],
                                               "score": score(case, retry["content"], retry["done_reason"])}
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
        if "reask" in result:
            result["reask"]["score"] = score(cases[result["case_id"]], result["reask"]["content"],
                                             result["reask"]["done_reason"])
    write_report(output, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=("ollama", "anthropic"), default="ollama")
    parser.add_argument("--base-url", help="Defaults to local Ollama or the Anthropic API")
    parser.add_argument("--api-key-env", default="ANTHROPIC_API_KEY",
                        help="Environment variable holding the API key (never a CLI value)")
    parser.add_argument("--prompt", action="append", metavar="NAME=PATH",
                        help="Additional system prompt arm to compare")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--reask", action="store_true",
                        help="Re-ask once after a model-attributable guardrail rejection, as the service does")
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
