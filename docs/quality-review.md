# Challenge quality criteria

This review addresses the challenge's three qualitative criteria: resilience to unexpected errors, prompt quality, and pipeline architecture. The implementation is appropriate for the supplied single-process exercise; its limits are explicit rather than presented as production guarantees.

| Criterion | Evidence | Boundary of the conclusion |
| --- | --- | --- |
| Unexpected errors | Automated tests inject exceptions during extraction and notification. The affected job becomes `failed`, a traceback is logged, and the same worker successfully processes the next job; a defensive outer handler also keeps bookkeeping errors from ending a worker. Malformed model output is repaired only when lossless (quotes, bare keys, a missing final brace) and otherwise fails closed. In the drained k6 run, 0 of 2,749 jobs hit an unexpected exception. Tests also cover shutdown cancellation, malformed HTTP `200` responses, timeouts, overload, and safe retry classification. | Notification timeouts can have an unknown external outcome. The provider has no idempotency key or lookup API, so those jobs are not replayed. |
| Prompt quality | The system prompt defines the success schema, complete body copying, recipient/channel rules, untrusted-input boundaries, one explicit refusal format, and positive/negative examples in Spanish. Prompts were compared on real models: 100 scored Qwen 3.5 4B calls and 960 Claude Haiku 5.5 calls (3 repeats). The current prompt reached 119/120, 60/60, 30/30 on the earlier sets and 54/60 on a set frozen before it was run (60/60 after the quotation guardrail), with zero incorrect acceptances. | The mock ignores the system prompt. Real-model evidence comes from the separate evaluation, whose small samples and two models do not establish universal accuracy or optimality. |
| Pipeline architecture | One bounded queue, a fixed worker pool, reusable HTTPX connections, atomic scheduling per ID, guarded states, bounded storage, and a shared notification limiter. Concurrent idempotency, worker limits, overload, and the complete HTTP adapter pipeline have executed tests. | State and throttling are local to one process and volatile across restarts. The notification quota remains the throughput bottleneck. |

## Message integrity

Checking that a model's message is a substring of the request prevents invented text but does not prove that it is the right text. The supplied mock exposes both failure modes:

- Without a colon it returns the first 50 lowercased characters of the request, e.g. `enviar email a juan@example.com diciendo que su pa`. That is a substring, so the earlier guardrails sent it. The message now must not overlap the recipient, which rejects it.
- With a colon it takes the text after the **last** colon, so `cita a las 10:00` becomes `00`. When a colon immediately follows the recipient, the body is explicit in the source; the earlier guardrails rejected the fragment, and the request failed. The full source body is now sent when the model's message is exactly the tail after a later colon and the body names no other contact.

Every accepted message is taken from the request itself, so the user's casing is preserved even though the mock lowercases. The design rule is: repair only when the correct value can be read from the request without interpretation, otherwise fail. Refusals, mixed command/body output and ambiguity stay `failed`; they are not counted as delivered just because k6 accepts their HTTP status.

## Architecture trade-offs

The same worker owns extraction, validation, throttling, and notification. This bounds jobs across all stages and keeps failures and state transitions in one place. More independent stage queues would add coordination without increasing the provider's long-run notification ceiling of five requests per second.

Queue capacity bounds memory, not completion latency. At that nominal ceiling, 4,096 sendable jobs represent roughly 13.7 minutes of notification capacity; bursts, extraction failures, and retries affect actual draining time. No delivery latency SLO is claimed. A production queue size should come from a chosen wait-time budget, with durable state and distributed rate-limit accounting if multiple processes are required.

## Evidence and prompt evaluation limits

The latest suite passed **161 tests** on Python 3.12. Executed load results and a processing sample are recorded in [benchmark.md](benchmark.md). Tests validate observable pipeline behavior and guardrails; assertions about the presence of prompt keywords would not demonstrate model quality.

The [real-model evaluation](llm-evaluation.md) measures exact outputs and downstream decisions on fixed datasets, including the k6 prompts. It exposed over-refusals of instruction-like bodies, quote stripping, false rejections and incorrect acceptances that deterministic simulator tests had missed. Original failures and re-scored outputs are retained. A larger untouched dataset and the actual production model would still be needed for a production accuracy claim; k6 itself remains an API availability test against the mock.
