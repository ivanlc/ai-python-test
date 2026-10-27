# Real-model evaluation

This optional tool sends synthetic extraction requests to a real model (local Ollama or the Anthropic API) and evaluates the returned text with the application's production guardrails. It never starts the notification service or calls `/v1/notify`.

## Run

Install the development requirements, then choose a backend:

```sh
# Local Ollama (model must already be pulled and served)
python -m evaluation.run --model qwen3.5:4b-q4_K_M \
  --include-baseline --prompt previous=evaluation/previous-prompt.txt \
  --output evaluation/results/my-run.json

# Anthropic API: the key is read from an environment variable, never from the CLI
export ANTHROPIC_API_KEY=...
python -m evaluation.run --backend anthropic --model claude-haiku-5-5 \
  --prompt previous=evaluation/previous-prompt.txt --repeats 3 \
  --cases evaluation/holdout2.json --output evaluation/results/my-run.json
```

`--prompt NAME=PATH` adds a comparison arm (repeatable); `--include-baseline` adds the first prompt from commit `ad2c1e8`. The `current` arm is always read from `app/ai/prompts/extraction_system.txt`. `--repeats N` runs every case N times, useful for models whose temperature cannot be fixed. `--reask` mirrors the service: when the guardrails reject unusable output (a format error) for a request that passes the pre-flight, the case is asked once more with the fixed reason, and `pipeline_correct_with_reask` / `incorrect_acceptance_with_reask` are reported next to the single-call scores. `--base-url` overrides the backend default. An existing result file is never overwritten, and the API key is not written to reports.

Datasets:

| File | Cases | Purpose |
| --- | ---: | --- |
| `cases.json` | 40 (24 valid, 16 refusals) | Main set: body integrity, quotes, Unicode, invalid recipients, channel conflicts, unsupported channels, multiple requests, injections, contacts inside bodies |
| `holdout.json` | 20 (12/8) | Separate set; one finding informed a later fix |
| `k6.json` | 10 (8/2) | The four prompts of the supplied k6 load test plus connector variants (`diciendo que`, `que`, `saying that`) |
| `holdout2.json` | 20 (12/8) | Frozen before the historical v3 prompt; regression data for later revisions |

The `qwen3.5-4b-delivery-*` reports were scored while `n11` (a WhatsApp request) temporarily expected SMS; that policy was reverted and `n11` again expects a refusal.

Each call uses a fresh system/user conversation. Case order is shuffled with seed 42 and arm order rotates. Concurrency is one and generation is limited to 512 tokens. Thinking is disabled on both backends. JSON mode, tools and prefill are deliberately absent: the prompt must produce valid output on its own, and a refusal remains plain text. Ollama additionally uses temperature 0 and a 4,096-token context; the Anthropic model used does not accept a temperature. Results are in [the real-model report](../docs/llm-evaluation.md).

## Scoring

- `raw_correct`: valid intents require strict JSON with exactly `to`, `message`, and `type`, and a correct payload. Negative cases require a clearly marked plain-text refusal. Email case and phone hyphens are normalized for recipient comparison; the body must match exactly, including case, punctuation, quotes, and newlines.
- `pipeline_correct`: valid intents must pass the real guardrails with the expected payload; negative cases must be rejected.
- `incorrect_acceptance`: an output passes guardrails although its accepted payload is wrong, or the request should have been refused. This means it **would be accepted**, not that a notification was sent.
- `valid_intent_rejected`: a valid request was rejected, including failures caused by model output. `guardrail_false_rejection` is the subset where the model supplied an already correct response.
- `model_error_blocked`: an incorrect model response was rejected by guardrails.

The raw scorer is separate from production parsing and checks the full expected payload. An empty model answer is scored as a model failure. The Anthropic backend retries `429`/`5xx`/`529` up to three times; any remaining transport failure aborts the run and preserves an explicitly incomplete report. Successful process exit means inference completed, not that every answer was correct. Each report retains source/prompt/dataset hashes, model settings, per-case output, latency, token counts, and decisions. Checkpoints are written after every call.

## Re-score existing outputs

After a guardrail change, preserve the generation evidence and create a new scored report without calling the model again:

```sh
python -m evaluation.run --rescore evaluation/results/original.json \
  --output evaluation/results/rescored.json
```

The report records generation and rescoring revisions separately. Changing a prompt requires fresh inference; rescoring cannot measure a different prompt.

These curated samples and two models do not establish general accuracy, robustness across models, or statistical confidence. Inspect failures and report model behavior separately from guardrail behavior. The deterministic evaluator itself is covered by ordinary tests, which need neither Ollama nor an API key.

References: [Ollama chat API](https://docs.ollama.com/api/chat), [runtime parameters](https://docs.ollama.com/modelfile), [Anthropic Messages API](https://docs.anthropic.com/en/api/messages).
