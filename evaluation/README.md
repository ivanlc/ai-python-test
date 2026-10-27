# Real-model evaluation

This optional tool sends synthetic extraction requests to a local Ollama model and evaluates the returned text with the application's production guardrails. It never starts the notification service or calls `/v1/notify`.

## Run

Install the repository's development requirements and use an available Ollama model:

```sh
python -m evaluation.run --model qwen3.5:4b-q4_K_M \
  --include-baseline --output evaluation/results/my-run.json
```

The model must already be installed and served by Ollama. The tool does not download models. `--base-url` defaults to `http://127.0.0.1:11434`; `--cases` can select a different dataset. An existing result file is never overwritten.

The frozen dataset contains 24 valid notification intents and 16 requests that should be refused. It includes body integrity, English/Spanish phrasing, quotes, Unicode, invalid recipients, channel conflicts, unsupported channels, multiple requests, attempts to override instructions, and recipient-like data inside message bodies. The baseline is the previous system prompt from commit `ad2c1e8`; the current prompt is imported directly from `app.provider`.

`holdout.json` contains 20 separate cases. The first run was made after initial fixes; one additional finding informed the last fix. Original results and final re-scoring are both retained, so final regression scores are not presented as untouched holdout accuracy. Executed results are in [the real-model report](../docs/llm-evaluation.md).

Each case is evaluated once per prompt with a fresh system/user conversation. Case ordering is shuffled with seed 42; prompt order alternates. Concurrency is one, temperature zero, generation is limited to 512 tokens, context is 4,096 tokens, and thinking/streaming are disabled. JSON mode and tools are deliberately absent: the prompt must produce its own valid output, and refusal remains plain text. These are evaluation-only Ollama parameters; the challenge provider's contract is unchanged.

## Scoring

- `raw_correct`: valid intents require strict JSON with exactly `to`, `message`, and `type`, and a correct payload. Negative cases require a clearly marked plain-text refusal. Email case and phone hyphens are normalized for recipient comparison; the body must match exactly, including case, punctuation, quotes, and newlines.
- `pipeline_correct`: valid intents must pass the real guardrails with the expected payload; negative cases must be rejected.
- `incorrect_acceptance`: an output passes guardrails although its accepted payload is wrong, or the request should have been refused. This means it **would be accepted**, not that a notification was sent.
- `valid_intent_rejected`: a valid request was rejected, including failures caused by model output. `guardrail_false_rejection` is the subset where the model supplied an already correct response.
- `model_error_blocked`: an incorrect model response was rejected by guardrails.

The raw scorer is separate from production parsing and checks the full expected payload. A transport failure aborts the run and preserves an explicitly incomplete report. Successful process exit means inference completed, not that every answer was correct. Each report retains source/prompt/dataset hashes, model settings, per-case output, latency, token counts, and decisions. Checkpoints are written after every call.

## Re-score existing outputs

After a guardrail change, preserve the generation evidence and create a new scored report without calling the model again:

```sh
python -m evaluation.run --rescore evaluation/results/original.json \
  --output evaluation/results/rescored.json
```

The report records generation and rescoring revisions separately. Changing a prompt requires fresh inference; rescoring cannot measure a different prompt.

This curated sample and a small quantized local model do not establish general accuracy, robustness across models, or statistical confidence. Inspect failures and report model behavior separately from guardrail behavior. The deterministic evaluator itself is covered by ordinary tests, which do not require Ollama.

References: [Ollama chat API](https://docs.ollama.com/api/chat), [runtime parameters](https://docs.ollama.com/modelfile).
