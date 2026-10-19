# Smart proxy + Relay — experimental

This mode forwards Codex Responses requests through a loopback proxy using saved ChatGPT subscription credentials. It does not replace `CODEX_HOME/auth.json`, stop apps, or create a separate local thread for each account. Cold rotation remains available when proxy mode is off.

**Compatibility is not yet verified against a real Codex desktop or VS Code extension.** Tests use fake upstream responses and HTTP parsing, not real subscription traffic. The subscription endpoint is a backend integration, not the public OpenAI API. Upstream behavior can change. This is not a guarantee of feature parity or unchanged reasoning quality.

## Start

From the project directory:

```sh
python3 dashboard.py --port 8766 --proxy-port 8767
```

Without `--proxy-port`, no inference proxy is started. The proxy binds only `127.0.0.1`. Open the dashboard at `http://127.0.0.1:8766/` and find **Smart proxy & Relay**.

When actually starting proxy mode, it creates a private `client-key` under `.relay-accounts/proxy/`. That key authenticates native local clients; it is not a provider credential. The dashboard shows its file path but never the key. Do not commit it or paste it into issues.

## Configure a test client explicitly

Do this only when ready to test; Relay does not modify your Codex config automatically. Back up your configuration first. Prefer an isolated test profile and a new thread, rather than an existing critical session.

A starting custom provider configuration in `config.toml`:

```toml
[model_providers.relay_proxy]
name = "Local Codex Relay"
base_url = "http://127.0.0.1:8767/v1"
wire_api = "responses"
env_key = "CODEX_RELAY_PROXY_KEY"
requires_openai_auth = false
supports_websockets = false

[profiles.relay_proxy]
model_provider = "relay_proxy"
# Keep your existing supported Codex model; no model names are rewritten by Relay.
```

Set `CODEX_RELAY_PROXY_KEY` in the environment of the test client using the contents of the private `client-key` file. For a Terminal-launched CLI, from the project directory:

```sh
export CODEX_RELAY_PROXY_KEY="$(cat .relay-accounts/proxy/client-key)"
codex -p relay_proxy
```

A GUI app launched through Finder may not inherit that environment. Configure the desktop/extension through its supported settings or launch environment and verify the actual endpoint. `OPENAI_BASE_URL` alone is not evidence that every client request uses the proxy. A one-time client reload may be required to apply the provider configuration; per-account route changes do not intentionally restart clients.

Do not inject ChatGPT OAuth tokens into the client provider configuration. Only Relay reads saved account credentials and places the selected credential on the upstream request.

### Required protocol behavior

- HTTP `POST /v1/responses`, `stream: true`, and a model name already supported by your account.
- Stable per-thread `session_id` or `X-Relay-Thread-ID` request header. If absent, proxy returns `session_required`; it does not mix requests under a global default session. Do not configure the same constant thread header for every conversation.
- Uncompressed JSON request with `Content-Length`, up to 16 MiB. HTTP WebSocket inference is not implemented.
- No model catalog or model translation endpoint yet; `/v1/models` is not implemented.
- `/v1/responses/compact` forwards native compaction on the current route. After the client receives a verified compacted response, the old proxy rebase anchor is cleared and the client must use that new context.
- The proxy does not rewrite unsupported tool schemas, model names, parameters or reasoning payloads to hide upstream incompatibility. Rejections are visible and are not retried on another account.

Config options depend on the installed Codex version. See [official sample configuration](https://learn.chatgpt.com/docs/config-file/config-sample) and [gateway compatibility](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility).

## Routing and quota

Each observed thread receives a persistent account binding. On first request, Relay prefers the selected local account if eligible, otherwise selects another verified candidate. This affects proxy routing only, not the local login file.

Quota is verified using Codex app-server, with a 30-second cache inside proxy mode. Saved profile refreshes use an isolated temporary Codex home. A global execution lease coordinates this with the dashboard collector and the legacy rotation engine. This first version serializes inference/checkpoint operations through that lease.

At the configured reserve threshold (normally 15% remaining), a new user request returns `handoff_required`. Tool-output follow-up requests can stay on the same account to finish the active loop. If all limiting windows reset within five minutes, Relay returns `waiting_reset` rather than switching.

**Automatic checkpoint/model calls are not enabled in this version.** The user confirms checkpoint creation in the dashboard. Legacy cold-rotation starts and enabling legacy automatic rotation are rejected while proxy mode is active, to avoid two engines managing the same work concurrently.

## Checkpoint and route change

1. Finish the current agent turn; do not start another task while creating a checkpoint.
2. In the proxy panel, identify the thread and account. Existing managed labels are reused when the observed ID matches a native thread; otherwise set a local proxy alias.
3. Choose automatic destination or a specific account.
4. Confirm **Tạo handoff & chuyển tuyến**. This consumes source-account quota.
5. Relay requests a Markdown handoff using observed model-visible history, without providing tools. This is a summary request, not a claim of native compaction.
6. After confirmed completion, save the Markdown with a checksum, recheck destination quota, and atomically commit the new account binding and context boundary.
7. The next client request continues on the new account. The local thread is retained.

Same-account request bodies are forwarded byte-for-byte, including `previous_response_id` and opaque reasoning items. Streams are forwarded without rewriting model/tool events. Only authentication and allowlisted metadata headers are supplied to the fixed upstream.

### Context rebasing after handoff

The proxy stores the last confirmed client history as an anchor:

- For a full-history client request, that exact anchor prefix is replaced by the handoff; later items are retained.
- The same mapping continues on the second and later requests, so old pre-handoff history is not silently reintroduced.
- If the first request references the old last response, the old reference is removed and replaced with handoff plus delta input.
- Subsequent references must match the new route's latest response. Old/unknown references are rejected.
- Matching ignores only top-level transport `id`/`status`; reasoning content and tool data are not flattened into text.
- If the client rewrites its history differently, return `context_mismatch` rather than guessing. Different client serialization may require a dedicated adapter.

A summary is inherently lossy. Encrypted reasoning/compaction state is not assumed portable between accounts. The proxy does not promise preserved prompt cache, lower quota cost or identical quality across route changes.

A thread started before the proxy observed its complete context may work in forwarding mode but cannot be checkpointed. Unknown `previous_response_id` history and unresolved item references are not treated as complete context. Pending tool calls and commentary-only boundaries also block checkpoints.

## Persistence, logs and recovery

All runtime data is inside the ignored `.relay-accounts/proxy/`:

```text
client-key            Private local client credential
server.lock           Single-process ownership
sessions/*.json       Account binding, raw/effective history, rebase boundary, state
handoff-*.md          Checkpoint summaries
events.json           Bounded metadata-only activity history
```

Session files contain private conversation content, including opaque reasoning items. They are protected local files, **not encrypted at rest**. They are intentionally separate from metadata-only activity logs. Context storage is bounded; exceeding the limit pauses the session rather than silently truncating it.

The dashboard exposes aliases, account names, response IDs and event categories, not raw prompts, response bodies or provider credentials. Request access logs are suppressed.

On client disconnect, upstream error, missing confirmed completion or server restart during an inflight operation:

- Mark the session `uncertain`.
- Do not retry or rotate automatically.
- Preserve prior confirmed state and saved handoffs.
- Reject further requests for that session until reviewed.

There is no one-click replay/recovery for uncertain proxy sessions in this first version. Inspect the journal and actual project changes; use a new controlled thread/checkpoint after review. Do not delete runtime files or resend a failed request blindly: tools may already have produced side effects.

## What remains to validate

- Real CLI, desktop and VS Code header/format behavior.
- Real quota attribution to account A/B.
- Full context replay, incremental response references, reasoning, tool loops and native compaction.
- Prompt-cache/performance effects after switching.
- Long-running streams, account expiry and provider changes.
- Broader native client context normalization where strict anchors differ.
- Automatic handoff orchestration with a verifiable user-turn boundary.
- WebSocket transport and model catalog support.

## Verification before relying on it

Start a disposable test thread. Confirm the proxy panel shows it, then stop the proxy: the next inference must fail. If it still succeeds, the client has another route. Test multi-step tools on A, make a checkpoint, route to B, then perform multiple later turns and compare behavior. Check account usage separately. Do not assume a successful first request proves context continuity.

## Developer checks

```sh
python3 -m unittest test_smart_proxy -q
python3 -m unittest -q
node --check ui/app.js
python3 build_standalone.py
```

Tests use fake upstream streams and in-memory HTTP connections. They do not log in, spend subscription quota or change a real client's account.
