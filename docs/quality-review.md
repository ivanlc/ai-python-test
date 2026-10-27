# Challenge quality criteria

This review addresses the challenge's three qualitative criteria: resilience to unexpected errors, prompt quality, and pipeline architecture. The implementation is appropriate for the supplied single-process exercise; its limits are explicit rather than presented as production guarantees.

| Criterion | Evidence | Boundary of the conclusion |
| --- | --- | --- |
| Unexpected errors | Automated tests inject exceptions during extraction and notification. The affected job becomes `failed`, a traceback is logged, and the same worker successfully processes the next job. Tests also cover shutdown cancellation, malformed HTTP `200` responses, timeouts, overload, and safe retry classification. | Notification timeouts can have an unknown external outcome. The provider has no idempotency key or lookup API, so those jobs are not replayed. |
| Prompt quality | The system prompt defines the success schema, complete body copying, recipient/channel rules, untrusted-input boundaries, one explicit refusal format, and positive/negative examples in Spanish. A separate local Qwen evaluation executed 100 scored real-model calls and compared the previous/current prompts. | The mock ignores the system prompt. Real-model evidence comes from the separate evaluation, whose small sample and quantized local model do not establish universal accuracy or optimality. |
| Pipeline architecture | One bounded queue, a fixed worker pool, reusable HTTPX connections, atomic scheduling per ID, guarded states, bounded storage, and a shared notification limiter. Concurrent idempotency, worker limits, overload, and the complete HTTP adapter pipeline have executed tests. | State and throttling are local to one process and volatile across restarts. The notification quota remains the throughput bottleneck. |

## Message integrity

Checking that a model's message is a substring of the request prevents invented text but does not prove completeness. The supplied mock takes text after the **last** colon, so the benchmark request ending in `cita a las 10:00` can produce only `00`.

When a colon immediately follows the recipient, the application can identify the body without guessing: everything after that separator is the explicit body, including later colons and quoted text. A model output containing only a fragment is rejected before notification. The prompt gives the same rule and an appointment-time example. Without such an explicit delimiter, the guardrail checks grounding while the model remains responsible for understanding the complete intended body.

This intentionally prefers a terminal failure over sending a known truncated message. The simulator remains unchanged, including its extraction limitations. Its refusals, irrecoverable JSON, and incorrect bodies are not counted as successful deliveries just because k6 accepts their HTTP status.

## Architecture trade-offs

The same worker owns extraction, validation, throttling, and notification. This bounds jobs across all stages and keeps failures and state transitions in one place. More independent stage queues would add coordination without increasing the provider's long-run notification ceiling of five requests per second.

Queue capacity bounds memory, not completion latency. At that nominal ceiling, 4,096 sendable jobs represent roughly 13.7 minutes of notification capacity; bursts, extraction failures, and retries affect actual draining time. No delivery latency SLO is claimed. A production queue size should come from a chosen wait-time budget, with durable state and distributed rate-limit accounting if multiple processes are required.

## Evidence and prompt evaluation limits

The latest suite passed **128 tests** locally on Python 3.9 and in the final Python 3.12 application image. Executed load results and a processing sample are recorded in [benchmark.md](benchmark.md). Tests validate observable pipeline behavior and guardrails; assertions about the presence of prompt keywords would not demonstrate model quality.

The [real-model evaluation](llm-evaluation.md) now measures exact outputs and downstream decisions on fixed datasets. It exposed false rejections and incorrect acceptances that deterministic simulator tests had missed. Original failures and re-scored outputs are retained. A larger untouched dataset and the actual production model would still be needed for a production accuracy claim; k6 itself remains an API availability test against the mock.
