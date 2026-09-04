# Compaction and cross-route checkpoints

Read this reference when automatic compaction fails, a model switch triggers compaction unexpectedly, or the 8318 compatibility service must be changed.

## What the two Codex paths mean

- Legacy v1: `POST /v1/responses/compact`. Codex installs the returned `output` array as replacement history.
- Remote v2: `POST /v1/responses` whose input contains `{"type":"compaction_trigger"}`. Codex requires exactly one output item with `type=compaction` and replays its `encrypted_content` later.

In current Codex Desktop/Core builds, `features.remote_compaction_v2=false` selects legacy v1. It is not a local summarizer switch. Because the hosted and compatibility endpoints may no longer implement v1, the result is commonly 404 or 501. See [openai/codex#42468](https://github.com/openai/codex/issues/42468).

CLIProxyAPI can route a v2 request while returning only ordinary reasoning/message items for a third-party model. Codex rejects that because there is no compaction item. See [CLIProxyAPI#4848](https://github.com/router-for-me/CLIProxyAPI/issues/4848).

## Why changing models can compact a short task

A route-boundary switch and token-pressure compaction are different triggers.

- A change within one upstream route may continue the existing history directly.
- A change between upstream routes may checkpoint before the first turn on the target model so provider-specific reasoning or encrypted state is not replayed unchanged.
- This can happen when current usage is far below both models' effective context windows. Increasing catalog context metadata does not fix it.

Diagnose with task events and catalog metadata. Compare the model before and after the switch, live route selection, last-turn token usage, and effective context window. Do not infer the trigger from the UI label alone.

## 8318 compatibility contract

The Python loopback proxy owns only the compatibility boundary:

1. Ordinary HTTP/SSE and WebSocket requests keep their payloads unchanged except for local Authorization/Host forwarding.
2. Native GPT/Codex v2 compaction is passed through.
3. A third-party v2 target is called again as a no-tools summarizer using that same target model. Transient failures may retry only on the same route within the fixed bound.
4. The non-empty summary is encoded as `ocx1:` plus Base64 UTF-8 and returned in exactly one `compaction` item with ordered SSE completion events.
5. A later request carrying that `ocx1:` item is decoded into a standard user summary message. Real OpenAI encrypted blobs remain opaque and unmodified.
6. When returning to native GPT, only an invalid third-party reasoning `content` field is removed; valid summary/encrypted state is retained.
7. Legacy v1 is also intercepted and converted into recent user messages plus one summary message, so an older process does not hit a retired upstream endpoint during migration.

Compaction bodies accept identity, gzip/x-gzip, deflate, and zstd with a 32 MiB decoded limit. The dependency-free zstd path uses Python 3.14's `compression.zstd`; run a real zstd v2 plus replay probe before claiming Desktop compatibility. If the current interpreter cannot decode an unfamiliar encoding, the proxy forwards the original body unchanged instead of blocking all Codex traffic with 415. That bypass preserves transport availability but does not by itself prove synthetic compaction compatibility. Health counters report protocol version, v1/v2 counts, successes/failures, replay decodes, and the last error type; they never expose bodies, summaries, accounts, or credentials.

The `ocx1:` envelope follows the public compatibility convention documented by [OpenCodex](https://github.com/lidge-jun/opencodex/blob/main/src/responses/compaction.ts). It is not presented as an OpenAI-encrypted blob.

## Safe rollout

Do not mutate a healthy 8318 service while still developing the candidate.

1. Finish all code changes.
2. Pass Python compilation and the complete offline test suite.
3. Read-only check: 8318 is listening, its LaunchAgent/user service is running, and `/v1/models` returns 200. If 8318 is absent, stop and ask the user.
4. Run `bridge.py compaction configure` without `--apply`.
5. Present the full config diff, current config SHA-256, planned transaction ID, backup paths, apply command, impact, acceptance checks, and rollback command.
6. Wait for explicit approval. Apply must reuse the approved SHA and transaction ID.
7. On macOS, asynchronous `bootout` must finish before bootstrap. The script waits up to 10 seconds and bounds bootstrap retries.
8. A failed apply restores files and the previous service definition, writes a failed transaction record, and stops. Do not improvise repeated service commands.
9. After a successful handoff, require service state `running`, `/v1/models` HTTP 200, and an actual config diff identical to the preview.
10. Fully restart Codex so the feature flag is loaded, then run the full matrix.

`local-compaction` is a deprecated alias for the guarded configure flow. It must never write `remote_compaction_v2=false`.

## Verification matrix

For every live catalog model, verify independently:

- legacy v1 returns replacement history and preserves an exact marker;
- v2 emits ordered created/added/done/completed events with exactly one compaction item;
- replay returns the marker from the checkpoint;
- an ordinary response still works;
- the target route, not a fallback Provider, handled synthetic summarization.
- a zstd-encoded v2 request emits the same single item and its zstd-encoded replay returns the marker.

Treat optional catalog entries with no live route as unavailable, never passed. A transient 429/5xx/EOF can be retried within the fixed same-route bound and should report its attempt count. Persistent failure blocks rollout.
