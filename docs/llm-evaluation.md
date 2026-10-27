# Real LLM evaluation

On 2026-10-27 the extraction prompts were evaluated against **Qwen 3.5 4B via local Ollama**, not the challenge simulator. No notification endpoint was called by the evaluator. All inputs are synthetic.

## Model and method

- Installed model: `qwen3.5:4b-q4_K_M`, 4.2B parameters, `Q4_K_M` quantization; local model ID prefix `d8b0f5e9760c`, Ollama CLI 0.40.10.
- Temperature 0, seed 42, context 4,096 tokens, maximum generation 512 tokens, thinking and streaming disabled, concurrency 1.
- Separate system/user messages through Ollama's `/api/chat`; no JSON mode, schema enforcement, tools, or notifications.
- Main set: 40 fixed cases, 24 valid and 16 requiring refusal. Both the previous prompt from `ad2c1e8` and the current production prompt were tested once per case: **80 scored model calls**.
- A separate set of 20 new cases, 12 valid and 8 requiring refusal, was frozen and run once with the current prompt after the first guardrail fixes: **20 additional scored calls**.
- Prompt, dataset, and validation-source hashes, raw answers, latency and token counts are retained in the reports. Case ordering uses a fixed seed and prompt order alternates.

There were **100 scored real-model calls and zero transport failures**. A preliminary smoke request is excluded from the scored datasets. The prompt was not changed after examining model outputs. Guardrail revisions were scored against the same saved outputs, without selectively repeating inference or changing expected answers.

## Main comparison

| Metric, 40 cases per prompt | Previous prompt | Current prompt |
| --- | ---: | ---: |
| Correct raw response | 21/40 (52.5%) | 31/40 (77.5%) |
| Correct pipeline decision before fixes | 32/40 (80.0%) | 36/40 (90.0%) |
| Incorrect payloads accepted before fixes | 3 | 1 |
| Correct model responses rejected before fixes | 2 | 2 |
| Correct pipeline decision after fixes | 37/40 (92.5%) | 39/40 (97.5%) |
| Incorrect payloads accepted after fixes | 0 | 0 |
| Correct model responses rejected after fixes | 0 | 0 |

The current prompt's remaining main-set failure is `p16`: the model omitted the second line of a valid message. The guardrail rejects that partial body. Its raw generation remains a failure in the final results; it was not repaired or rerun to improve the score.

Raw success requires the expected complete body, including case, punctuation, quotes, and newlines. Merely returning parseable JSON does not earn credit. Recipient case/phone hyphens are normalized for comparison. Negative cases require a clearly marked plain-text refusal. See the [evaluator's scoring definition](../evaluation/README.md).

## Separate cases and the additional finding

The first run of the 20 new cases produced **16/20 correct raw responses (80%)** and **19/20 correct pipeline decisions (95%)**, with one incorrect acceptance. The model treated `con un mensaje positivo.` as the literal body of a request that had supplied only a description of a desired message.

The description rule was extended to cover those connectors. Re-scoring the **unchanged** 20 outputs produces 20/20 correct pipeline decisions and zero incorrect acceptances. Because that finding informed the last correction, this final 20/20 is regression evidence, **not a fresh independent estimate of accuracy**. The original 19/20 result remains published.

## Production fixes supported by these observations

1. Separate an explicit recipient-adjacent body delimiter before resolving recipients. An order number or a contact mentioned in the body must not become another destination. A model cannot substitute that body contact for the actual recipient.
2. Reject an entire command or an unprovided message description as notification content. Literal quoted, introduced, or delimited messages containing the same words remain supported.
3. Reject known unsupported channels instead of silently translating a WhatsApp, Slack, fax, or similar request into SMS.

With the final guardrails there are no incorrect acceptances among the **100 recorded outputs**. This is an observation about this finite regression sample, not a general safety guarantee. No real notification was sent in any case.

## Latency and limits

Main-set local inference median/p95 was 0.445/0.533 seconds for the shorter previous prompt and 0.604/0.713 seconds for the current prompt. The new-case run was 0.623/1.003 seconds. These are warm local measurements with thinking disabled; they are not production latency or cross-model performance claims.

This is a small, curated, predominantly English/Spanish dataset and one quantized model at one generation setting. The guardrail changes were informed by observed failures. The reported final regression scores do not establish performance on arbitrary language, other model sizes/providers, or repeated stochastic generations. Descriptive-body and channel checks are conservative language rules, not a complete semantic intent classifier. Further production validation should use additional untouched data and the actual selected provider.

## Reproduce and inspect

Follow [evaluation/README.md](../evaluation/README.md). The original challenge provider, Compose services, and evaluation scenarios remain unchanged.

| Evidence | File |
| --- | --- |
| Main raw generations and original scores | [qwen3.5-4b-initial.json](../evaluation/results/qwen3.5-4b-initial.json) |
| Same main outputs, final guardrails | [qwen3.5-4b-final-main.json](../evaluation/results/qwen3.5-4b-final-main.json) |
| New cases, first run before connector fix | [qwen3.5-4b-holdout.json](../evaluation/results/qwen3.5-4b-holdout.json) |
| Same new-case outputs, final guardrails | [qwen3.5-4b-final-holdout.json](../evaluation/results/qwen3.5-4b-final-holdout.json) |
