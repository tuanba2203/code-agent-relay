# Codex Relay — local dashboard and integration core

Status: live quota dashboard, encrypted account backup, per-account polling,
and journalled cold rotation with real official app-server compact / handoff import.
**Hot switching and GUI control of an already-running desktop/VS Code conversation
are not implemented.** External clients must be closed before execution. Relay can
optionally continue one local-work turn in its own app-server after importing context;
it does not automatically select that thread inside an external client.

## Test compact → handoff → rotate

1. Run `python3 dashboard.py --port 8766` in a separate macOS Terminal and open
   `http://127.0.0.1:8766` in Safari/Chrome (not the browser inside the app you will close).
2. Save local work. Both clients must share the same CODEX_HOME and explicit
   `cli_auth_credentials_store = "file"`. CLI must be installed; configure accounts first.
3. Under **Compact · bàn giao · đổi tài khoản**, choose **Chọn chat & cấu hình**.
   Select up to 10 local conversations. Start with automatic mode and continuation OFF.
4. Choose **Force compact & export** to test checkpoint creation without changing account,
   or **Rotate ngay** for the complete cold-rotation pipeline. Close Codex desktop,
   VS Code and all Codex CLI processes when the dashboard says it is waiting.
5. Progress is visible in the browser. Real model calls consume quota. Target quota
   is checked before compaction and again before switching. Original rollout files and the local session database
   are backed up; compact completion must be confirmed before summarization/export.
6. Handoffs are private Markdown files with checksums in `.relay-accounts/rotations/`.
   Import reuses the managed thread already bound to the target account (`thread/read` +
   `thread/resume` + `thread/inject_items`). If no binding exists, it creates a new
   thread with `thread/start`, records the binding, then injects the handoff.
   After completion, reopen the desired client and select the new thread ID shown.
   CLI can resume a returned thread with `codex resume <thread-id>`.
7. Optional **tiếp tục** starts one turn in Relay's own app-server: workspace writes
   only in the selected project, network disabled, approval requests denied. This is
   not a turn running inside the desktop/VS Code UI. Results are saved as
   `continuation-XX.md`. If more permissions are needed, continue interactively later.
8. Optional automatic mode starts the same pipeline when the live plan requires a
   handoff. It waits for clients to close; it never kills applications. It follows
   the same managed workflow IDs after successful rotation, resolving the account–thread
   pair each time (including when returning to an earlier account). Restarting the server
   disables automatic mode until explicitly re-enabled.

During compaction and handoff generation, Relay logs a heartbeat every 30 seconds
with elapsed time, event inactivity, event count and response character count.
The private journal records the current turn ID and wait diagnostics. Server errors
are recorded as fixed error codes, without raw server messages or model output.
Each completion wait has a 600-second deadline; timeout or cancellation attempts
to interrupt the turn, and an interrupt failure does not hide the original cause.
Retry notifications do not start another turn. A non-retryable error stops the job.

Cancellation interrupts a known active turn before closing its app-server. A failed
compaction leaves the source account active. Failure after activation attempts to
restore the source account, retaining handoffs and import IDs. Local project edits
already made by continuation are NOT reverted. Interrupted or ambiguous jobs require
inspection and **Xác nhận sau phục hồi**; there is no automatic replay. If the account
manager itself has a pending switch journal, use its restore action first. The
complete journal and original rollout backups remain on disk; sensitive conversation
content stays local except for normal model requests to OpenAI for compaction/summary.
This implementation cannot freeze new input in unrelated running clients; keep them
closed until the job finishes. Remote/cloud-only histories and separate VS Code
CODEX_HOME directories are not covered by this local catalog.

## Live mode: current local login and real quota

Run:

```text
python3 dashboard.py --port 8766
```

Open `http://127.0.0.1:8766` in Safari or Chrome. Default mode now captures the current
ChatGPT file login from CODEX_HOME (normally ~/.codex), preserves an existing profile
name, enables account management and schedules quota reads per account using the rules below. The old
static HTML file cannot read credentials and is not the live dashboard.

The collector starts the installed official `codex app-server` with an isolated,
private temporary CODEX_HOME. It uses documented `account/read`,
`account/rateLimits/read` and optional `account/usage/read` requests. It starts no
model turns, performs no desktop UI control and uses no direct undocumented backend
HTTP endpoint. API errors show missing quota, never substituted sample values or zero.

During normal quota monitoring, only metadata reaches the browser: quota windows, reset times, plan and any available
lifetime/peak daily token totals. Dollar costs remain unavailable. Token totals are
not a conversion into remaining subscription quota. Each profile's own measurement
time controls freshness; a successful read of another account does not refresh it.

Credentials are copied into the temporary private home for the quota reader. Any
refreshed credential is saved only back to its own profile, after identity checks;
the collector never writes the active global auth file. It reads the active file to
prefer the current client's latest token. Expired/revoked profiles can still require
sign-in again. Token refresh by independent clients is not made atomic across those
clients; avoid parallel profile automation outside this manager.

Automated tests use fake credentials and protocol transports; they do not run model
turns or change the user's account. Live quota and local UI have been verified. The
cold-rotation engine still needs the user's end-to-end test with clients closed.
Current frontend identity is based on local auth, not live GUI inspection.

## Account switching (new implementation)

The account manager can now capture ChatGPT file logins, authenticate new profiles
through the official CLI, and replace a shared Codex auth file with a saved profile.
This is **offline switching**, not hot switching. Save work and close desktop,
VS Code and all Codex CLI sessions before switching; reopen them afterward. Both
frontends must use the SAME CODEX_HOME and file-credential mode. The manager cannot
verify a running frontend's identity or change macOS Keychain credentials.

### Start the management UI

Run in this project folder, then open the printed URL in a separate Safari/Chrome:

```text
python3 dashboard.py --manage-accounts
```

Select **Quản lý đăng nhập**. You can save the current login under a readable name,
view saved profiles and select a profile. Close the Codex app and VS Code before
clicking a switch button. The Python dashboard can remain running outside those apps.
The static HTML dashboard remains read-only and disables account-management controls.
The account-manager switch button only switches auth. Use the rotation panel for compaction and handoff.

### Configure file auth deliberately

Before switching, the shared Codex config needs the following TOP-LEVEL setting:

```toml
cli_auth_credentials_store = "file"
```

Then sign in again using the official client to create an `auth.json` file.
The manager refuses implicit/auto/keyring mode rather than silently changing
credential storage. It also refuses inherited `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`
or `CODEX_API_KEY` overrides during switching. Administrator-enforced auth policies
still take precedence. Do not change managed workspace policies to make this work.

### Add and select profiles from Terminal

```text
python3 accounts.py login --name "Work 1"
python3 accounts.py login --name "Work 2"
python3 accounts.py list
python3 accounts.py switch PROFILE_ID
```

`login` starts the official `codex login` in an isolated temporary CODEX_HOME and
saves the resulting file credential. Complete browser sign-in yourself; do not
paste tokens into chat or into the UI. It does not replace the global current login.
`capture --name "Current"` saves an existing current file login instead.
`list` returns metadata and an opaque ID, not credentials. Local JWT claims are
used only for profile deduplication/labels; they are not verified entitlement proofs.

Default storage is `.relay-accounts/` in this project, ignored by Git. Credentials
and backups are plaintext files with mode 0600 under a 0700 directory; this is file
auth storage, not an encrypted vault. Keep that directory private and out of archives.
`--directory` and `--codex-home` global options allow explicit alternative paths.

Each switch checks processes conservatively, requires a known outgoing login,
preserves outgoing token refreshes, saves a private backup, journals the change,
atomically replaces only `auth.json`, then verifies its bytes. It never stops apps,
rewrites config, modifies session databases or approves actions. Process checks are
best-effort observations, not an exclusive OS lock; do not launch clients midway
through a switch. A refresh or independently changed login aborts switching.

If a write fails, a pending journal blocks further operations. With clients closed:

```text
python3 accounts.py restore
```

Recovery refuses to overwrite an independently changed login. Backup files remain
for manual inspection and are not automatically pruned. The UI shows **selected on
disk**, not a claim that either client has accepted the identity after restart.

Validation: 69 tests passed, including temporary fake credentials, private file
permissions, snapshot tampering, client/keyring/environment guards, partial writes,
recovery, HTTP opt-in and CSRF. The account-switch tests use fake tokens. Separately, the live collector has been
verified using the current local login without replacing the active auth file.
The live desktop/browser restrictions recorded below still apply; there is no claim
of end-to-end verification against the user's real accounts.

## Open the dashboard

Open **`Codex Relay.html`** in Safari or Chrome. It is a single standalone file;
no server, installation, external fonts, hosting or network access is required.
Its Content Security Policy disables network connections. The standalone file starts empty and never loads real credentials. Use the local
server below for real accounts. Sample accounts appear only when explicitly requested.

Features:

- Five-hour and weekly remaining quota, reported reset times, snapshot age and
  current account from metadata. Missing quota, token and cost values are not
  fabricated. These are quota utilization percentages, not measured token totals.
- Per-account exclusion from rotation, adjustable handoff reserve and entry
  margin, two strategy options, ranked candidates and reasons.
- A handoff-plan dialog and downloadable JSON plan. These do not execute switches.
- Import of a metadata-only schema-1 snapshot envelope using the reference bridge
  contract below. Credential-bearing fields are rejected. Do not import auth.json.
- All imported data and preferences remain in memory for the current page session;
  reloading the standalone file resets to an empty offline view. There is no persistent account vault.

The optional Python local server reads an externally supplied snapshot file:

```text
python3 dashboard.py --snapshot /absolute/path/to/quota-snapshot.json
```

It prints a `http://127.0.0.1:8765` URL. Without `--snapshot` it connects to saved real accounts; `--demo` explicitly opts into sample data.
The server binds loopback only, validates Host/Origin, uses a per-process CSRF token
for state changes and logs no account metadata. Account-management endpoints are disabled
unless started with `--manage-accounts`.
Snapshot mode rereads metadata. Default mode collects actual quotas every 30 seconds.
The server and standalone implementations share equivalent planning behavior.

### Rotation policy

Default **reset-first** policy keeps the current account while its quota is usable,
then ranks eligible replacements using remaining allowance AND approaching resets.
Both the dashboard and real executor use `quota_policy.py`; the standalone JS view
has parity-tested equivalent logic. The executor reads fresh candidate quotas.

- Entry requires more than 30% remaining in BOTH five-hour and weekly windows,
  or a higher reserve + entry margin if configured. A reset bonus cannot bypass this.
- Score (0–100): weekly remaining × 0.45; weekly remaining × reset urgency × 0.20;
  five-hour remaining × 0.20; five-hour remaining × reset urgency × 0.05;
  lowest remaining allowance × 0.10. Weekly capacity carries the greatest weight.
- Reset urgency is `max(0, 1 - seconds_until_reset / window_seconds)` for a future,
  reported reset. Missing/past timestamps earn no bonus and never imply replenishment.
- The displayed reset advantage is the sum of the two reset-related score terms.
- Disabled, stale or failed-refresh accounts are excluded. Switching still waits for
  closed clients and revalidates quota after compaction. With no candidate, pause.
- **Balanced** remains available to rank only by the lowest remaining allowance.
  Existing reserve, margin and disabled-account settings are preserved.

This is a transparent heuristic to reduce wasted allowance while retaining session
headroom, not a prediction of future task duration or a globally optimal schedule.

Validation: **44 tests passed** at this stage, including 19 cross-language planning
scenarios within one parity test. HTTP request handling was tested with in-memory
transports because permission to bind a local port was not granted. Browser visual
QA could not run: the browser tool rejected the `file://` protocol. No workaround
to either restriction was attempted. Layout is responsive in source but has not
been visually verified in this environment.

## Requested behavior

Manage four distinct authorized ChatGPT accounts. When the active account crosses
a configurable quota threshold (default 92% used), prepare a handoff, change the
active account in Codex desktop on macOS AND the Codex VS Code extension, then
prepare a new session using that handoff.

Both the short-window quota and weekly quota must be checked. Percentage usage
can jump; 8% reserved is not a guarantee that summarization will succeed. All
accounts exhausted means pause, not an infinite retry loop. Quotas remain separate.

## What is implemented

`coordinator.py` defines a frontend-adapter contract and a two-phase coordinator:

1. Require distinct account identities and fresh verified quota.
2. Freeze new work across both frontends and drain active tools.
3. Summarize every managed thread and save private, atomic handoff files.
4. Recheck the target account's quota.
5. Activate the target and require both frontends to confirm that identity.
6. Import handoffs as drafts/new chats, without blindly replaying tasks.
7. Keep a transaction journal and attempt rollback on partial activation/import.
8. On failure, retain checkpoints and leave the frontends paused for recovery.

The adapter is a Python Protocol, not a working connection to the apps. Automatic
task continuation is intentionally absent until a real frontend can verify tool
completion, partial side effects and permissions. The adapter must enforce timeouts
and one cross-process lock for all frontends before deployment. The core does not
itself provide an interprocess deployment lock or redact arbitrary model output;
the real summary adapter must remove secrets before returning a handoff.

## Findings from this Mac

- Desktop bundle: `/Applications/ChatGPT.app`, identifier `com.openai.codex`.
- Local CLI: `0.163.0-alpha.10.2`.
- VS Code extension: `openai.chatgpt-26.1007.21434-darwin-arm64`.
- Extension source starts its own app-server over stdio and inherits `CODEX_HOME`.
  A separately started CLI process is therefore not automatically the extension's
  controller. Changing one process's auth does not prove another reloaded it.
- Extension setting `chatgpt.cliExecutable` is explicitly development-only. It is
  not changed by this prototype.
- The computer-use tool refused access to `com.openai.codex` for safety reasons.
  No fallback UI automation or alternate live-control route was attempted.

## Integration still required

Obtain a supported authenticated interface to each existing frontend. Verify
whether it supports exclusive quiescence, live thread subscriptions, account
switching, identity confirmation and handoff import. A standalone app-server's
`account/rateLimits/read` and `thread/compact/start` are documented, but do not
establish remote control of existing desktop/extension sessions.

Do not overwrite `~/.codex/auth.json` while desktop or VS Code is running. File
swapping does not account for in-memory authentication, keyring storage or token
refresh races. Do not copy SQLite state between active instances. Do not treat
internal compacted history as an exported handoff document.

Account onboarding requires the user to sign into each account themselves through
the official login flow. Never ask for tokens in chat. Before unattended deployment,
validate the installed frontend versions using disposable project/account sessions,
then test a real handoff and verify both account identities. The agent could not
perform that validation in this session because desktop access was blocked.

## Review of Loongphy/codex-auth (18 October 2026)

Source pinned to commit
[`05d50bc4b613d36e2799e381a2640050766b679e`](https://github.com/Loongphy/codex-auth/tree/05d50bc4b613d36e2799e381a2640050766b679e).
The source archive was downloaded and read as data only. No upstream executable,
install, build, login, switch or app command was run. No upstream source was copied
into our implementation. The reference project is MIT licensed; preserve its notice
if code is reused in a later version.

Findings:

- Ordinary switching backs up the live `auth.json`, copies the selected account
  snapshot into it, then changes registry state. The README requires restarting
  official CLI and desktop clients for the new account to take effect.
- Experimental desktop switching is a materially different route: `app` sets
  `CODEX_CLI_PATH` to a managed `Loongphy/codext` backend. Without an explicit binary,
  it queries the latest release, downloads and extracts it. The reviewed download
  path compares version metadata but does not visibly verify a pinned cryptographic
  digest before installation. This is a review observation, not an allegation of
  malicious behavior or a full audit of codext itself.
- The author labels that desktop integration unstable and warns that app changes
  can break it. It is evidence of an experimental implementation route, not proof
  that seamless switching works on this installed desktop build.
- VS Code is listed as supported, but a successful auth-file copy does not confirm
  that an already-running extension has adopted the new identity.
- No compact/export-handoff/import-context workflow was found in the reviewed
  codex-auth sources. The codext backend was not downloaded or reviewed.
- Usage is refreshed using direct ChatGPT backend endpoints. The JSON contract
  distinguishes a successful API refresh from local/cached data and failed refresh.
  An equal API response can leave `updated_at` unchanged, so snapshot age alone is
  insufficient; track the trusted caller's receipt time and refresh status.
- Its remaining-quota display can treat a past reset timestamp as full allowance.
  Our planner instead rejects that window until fresh data confirms recovery.

Safety: the complete source tree contains no `.vscode`, `.idea`, `.fleet`, workspace
task files, devcontainer, direnv, repository-managed hooks or npm lifecycle install
scripts. The archive has no `.git` metadata, so it cannot establish anything about
hooks in a separate local Git checkout. Build files compile Zig code; npm's launcher
executes a platform binary; GitHub workflows run build/release jobs on GitHub, not
merely on local open. Application launch and account commands have credential and
process side effects. It appears suitable for static inspection, but that is not
approval to run it against real accounts. Avoid automatic acceptance of backend
downloads, backend replacement and live-auth changes until that path is reviewed.
User-local IDE extensions and tooling remain outside this repository inspection.

### Read-only bridge now implemented

`reference_bridge.py` independently parses the documented schema-1 list response
and produces a rotation plan. It uses stable `account_key` identities, checks API
refresh success and both reported windows, rejects stale/ambiguous data, and selects
the candidate with the best minimum remaining allowance. The bridge itself never runs codex-auth, reads auth files, changes accounts, launches
a GUI or copies tokens. The separate opt-in `accounts.py` module manages file auth.

The input envelope is:

```json
{
  "received_at": 1792281600,
  "result": {"schema_version": 1, "command": "list", "active_account_key": "...", "accounts": []}
}
```

The trusted live caller must capture `received_at` immediately when a successful
`list --api --json` invocation completes. Do not stamp old saved output with a new
receipt time. This prototype does not provide or execute that live caller.
For a current snapshot, `python3 reference_bridge.py --snapshot snapshot.json`
prints a plan with `executed: false`; it does not perform the plan.

Reference evidence:
[switch behavior](https://github.com/Loongphy/codex-auth/blob/05d50bc4b613d36e2799e381a2640050766b679e/docs/commands/switch.md),
[experimental app launch](https://github.com/Loongphy/codex-auth/blob/05d50bc4b613d36e2799e381a2640050766b679e/docs/commands/app.md),
[JSON contract](https://github.com/Loongphy/codex-auth/blob/05d50bc4b613d36e2799e381a2640050766b679e/docs/json-api.md),
[download implementation](https://github.com/Loongphy/codex-auth/blob/05d50bc4b613d36e2799e381a2640050766b679e/src/workflows/app.zig).

## Quản lý tài khoản trên dashboard

Mở `http://127.0.0.1:8766` rồi chọn **Quản lý đăng nhập**:

- **Lưu đăng nhập hiện tại** nhập tài khoản Codex local vào Relay.
- **Thêm tài khoản qua đăng nhập** tạo một phiên đăng nhập chính thức riêng;
  mở liên kết và tự hoàn tất đăng nhập để lưu tài khoản mới.
- **Lưu alias** đổi tên gợi nhớ. Xóa nội dung alias để dùng email.
- **Gỡ profile** yêu cầu xác nhận, xóa profile và credential đã lưu trong Relay.
  Đăng nhập local và tài khoản OpenAI không bị xóa; bản sao phục hồi cũ vẫn được giữ.

Alias được ưu tiên hơn email trên bảng quota, account đang sử dụng và đề xuất rotate.
Profile đã gỡ không tự xuất hiện lại khi khởi động server; có thể chủ động lưu lại.
**Export / Import:** nhập mật khẩu ít nhất 12 ký tự trong cửa sổ quản lý,
rồi chọn Export hoặc chọn file Import. File dùng AES-256-GCM, khóa dẫn xuất bằng
PBKDF2-SHA256 (600.000 vòng, salt và IV ngẫu nhiên). Mật khẩu không gửi tới server,
không lưu lại. File chứa phiên đăng nhập và alias, không chứa lịch sử chat.
Credential chỉ đi qua trình duyệt local trong lúc export/import. Giữ mật khẩu riêng;
không có chức năng khôi phục mật khẩu. Import giữ nguyên profile trùng trên máy,
không đổi đăng nhập hiện tại. Phiên hết hạn cần đăng nhập lại.

Mỗi account có lịch đọc quota riêng. Với account đang dùng, ưu tiên kiểm tra
mốc quota thay đổi: nếu quá 30 phút không đổi thì đọc mỗi 30 phút. Nếu quota có
thay đổi trong 30 phút gần đây, lấy phần còn lại thấp nhất giữa quota 5 giờ và tuần:
trên 50% đọc mỗi 5 phút; từ 30–50% đọc mỗi 2 phút; dưới 30% đọc mỗi 30 giây.
Account dự phòng hoặc bị tắt tham gia rotate đọc mỗi 30 phút. Không đổi quota là
một suy đoán không hoạt động, không phải bằng chứng chắc chắn. Mốc theo dõi đầu
được tính từ lần đọc đầu; dữ liệu quota và mốc thay đổi lưu trong quota-history.json,
không chứa credential. Khởi động lại luôn đọc một lần để xác minh dữ liệu mới.
Dashboard và bộ điều phối vẫn kiểm tra cache local tối đa mỗi 30 giây để nhận ra
thay đổi đăng nhập, nhưng chỉ gọi OpenAI cho account đến hạn. Mỗi hàng hiện giờ
đọc gần nhất, mốc quota không đổi và giờ đọc tiếp. Dữ liệu quá interval riêng của
account cộng 60 giây không được dùng để đề xuất chuyển. Nút đọc lại buộc đọc tất
cả account ngay; bật lại account hoặc chuyển account đang dùng cũng xác minh ngay.
Lỗi đọc account đang dùng thử lại sau 2 phút; account khác giữ lịch 30 phút.

Việc chuyển file đăng nhập vẫn yêu cầu đóng Codex desktop, VS Code và CLI trước.
Compact và bàn giao dùng panel rotate; chuyển nóng trong client đang chạy chưa được hỗ trợ.

## Run offline tests

From this folder, run `python3 -m unittest -v`. Python tests require Python 3.9+
and the standard library; JavaScript parity tests additionally use Node.js and
skip explicitly if it is unavailable. Tests never contact OpenAI or access account
credentials. Rebuild the standalone file after UI edits with
`python3 build_standalone.py`.

## Safety inspection

The task directory was initially empty except for `work/` and `outputs/`. No IDE
configuration, devcontainer, shell auto-load file, active Git hook, bootstrap
script, installer or repository auto-run vector was present. This deliverable has
no install hooks or third-party dependencies. IDE extensions and user-local tools
can still introduce behavior outside these files.

## References

- [App-server protocol](https://learn.chatgpt.com/docs/app-server)
- [Authentication and credential storage](https://learn.chatgpt.com/docs/auth)
- [VS Code developer settings](https://learn.chatgpt.com/docs/developer-settings?surface=ide)

Voucher eligibility, multi-account terms and plan benefits are outside what this
prototype validates.

## Refresh và countdown

Mỗi hàng tài khoản có nút Refresh để buộc đọc lại quota riêng account đó,
không chờ lịch tự động. Nút Đọc lại dữ liệu ở đầu trang đọc lại tất cả account.
Trạng thái hiện countdown phút:giây và chuyển sang Đang kiểm tra khi người dùng
yêu cầu refresh; khi hoàn tất, hiển thị số liệu mới và countdown theo rule mới.
Chỉ hiển thị quota Codex; không có bảng quota ChatGPT.

## Managed workflow registry

Selecting a new local conversation registers a logical work stream with a stable
workflow ID and editable label. Each workflow stores project, per-account thread IDs,
ready/prepared import state, and the most recent 100 rotation outcomes. The private
registry lives at `.relay-accounts/rotations/managed-threads.json`; the previous
version is retained alongside it. Full job journals remain in each rotation folder.

Example: XXX → A:111, B:112, C:113. A→B→C→A reuses 111 on returning to A.
YYY has a separate mapping and history. Newly created target threads are registered
before context injection, so a failed injection retains an identifiable prepared
thread instead of losing track of it. A failed/missing/wrong-project tracked thread
stops the rotation; the tool does not silently create a duplicate.

The dashboard shows each workflow's label, account/thread pairs and recent history.
You may rename the label or explicitly link an existing local thread on another
configured account in the same project. An existing binding is not silently replaced.
The selection dialog separates managed workflows from unregistered local chats.
Legacy physical-thread selections are migrated when saved or first started.
An unreadable registry blocks rotation rather than recreating an empty registry.
The handoff is appended as the latest progress snapshot; older target conversation
history is retained. Acknowledged injection is recorded before optional continuation.

### Shared alias and native thread names

A managed workflow label is the common alias for its whole account/thread chain.
Native Codex titles use that exact alias, with no per-account suffix. New and reused
target threads receive `thread/name/set` before importing context. Changing an alias
queues name synchronization for every linked thread, not just the active account.
The UI shows pending/synced/error name state per binding and provides a retry button.
Pending names are synchronized after external clients close; this metadata operation
starts no model turns and does not switch authentication. Successful acknowledgements
are persisted; an acknowledgement for an older alias cannot mark a newer alias as synced.

Automatic rotation is gated by the ACTIVE account, independently of target ranking.
The default handoff reserve is 15%: keep the current account above 15% remaining
in both windows; at 15% or less in either window, find/revalidate a target and queue
the handoff pipeline. A favorable reset on another account never triggers an early
switch. The manual Rotate action remains an explicit override for testing.

Before automatic handoff, the current account gets a five-minute reset grace period.
Wait only if every window at/below the reserve resets within five minutes. A short
window resetting soon cannot override an exhausted weekly window resetting later.
The collector schedules a fresh read just after reset (subject to the worker wakeup),
even during idle polling; timestamps alone never imply replenished quota. The executor
rechecks the source before compaction and cancels unnecessary handoff if quota has
recovered or reset is imminent. Manual Rotate remains an explicit override.

Account không có alias thủ công dùng alias tự động `email_localpart_account_type` (ví dụ `user_plus`, `user_team`). Business chuẩn hóa thành `team`; metadata thiếu loại gói dùng `unknown`. Alias thủ công được giữ nguyên; xóa alias thủ công sẽ trở về alias tự động. Tên thread có dạng `workflow · account_alias`.
