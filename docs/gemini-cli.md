# Google Gemini via native CLI / OAuth

Relay supports local Gemini CLI profiles alongside Codex profiles. This is a
native-client integration: the official `gemini` executable performs Google
sign-in, token refresh and inference. Relay does not extract OAuth credentials,
call Google backend services directly, or forward Google tokens through the
Codex Responses proxy.

## Dashboard

Start the existing live dashboard and find **Gemini · Đăng nhập Google**.
Create a named profile, expand its sign-in instructions and run the displayed
command in Terminal. Choose **Sign in with Google** in the official CLI.
Creating a profile only writes local metadata and native settings; it does not
start authentication or make a model request.

The CLI must already be installed from the
[official installation guide](https://geminicli.com/docs/get-started/installation/).
Relay does not install packages automatically. The CLI must be on the Terminal's
PATH; the dashboard checks its own PATH separately, which can differ.

Each profile uses `GEMINI_CLI_HOME=<accounts-dir>/gemini/<profile-id>`, and the CLI
stores its `.gemini` configuration there. `GEMINI_CLI_HOME` points to the parent
of `.gemini`, as described in the
[configuration reference](https://geminicli.com/docs/reference/configuration/#environment-variables-and-env-files).
Relay writes `security.auth.selectedType=oauth-personal` for a new profile. It
does not modify the default `~/.gemini`, `~/.codex`, or live Codex account.

Rename a profile or archive it from the panel. Archiving blocks new launches
through Relay and keeps all native data. Restoring the profile enables launches
again. Archiving does not stop a running client or revoke Google access.

## Terminal

```sh
python3 gemini_provider.py create 'Google Work'
python3 gemini_provider.py list
python3 gemini_provider.py login PROFILE_ID
python3 gemini_provider.py run PROFILE_ID --project /path/to/project
```

For a dashboard started with a custom `--accounts-dir`, use the full generated
commands: they include the matching Gemini `--directory` argument. Profile IDs
are local configuration identifiers, not verified Google account identifiers.
Aliases are user supplied; Relay does not claim that sign-in has completed.

The launcher starts the interactive CLI without a shell and passes through its
exit code. Only one Relay-launched client per profile can run at once. It does
not automatically retry, rotate or resume failed requests. Google API keys,
ADC/Vertex overrides and custom API base URLs are removed from the launch
environment. Google Cloud project variables are retained because some managed
Google accounts require them. Login starts in the profile home; work starts in
the project directory you explicitly provide.

The native CLI may load project settings, `.env`, hooks, MCP servers, extensions
and tools. Review the project before starting it, and retain native approval
prompts. This launcher is not a sandbox. It does not enforce or verify a
particular Google subscription, CLI keychain behavior or endpoint at runtime.

## Quota and context

Use `/stats model` in the Gemini CLI for its quota information. Relay reports
Google quota as **unknown**; it does not apply Codex 5-hour/weekly windows or
invent reset timestamps for Google. See Google's
[quota documentation](https://geminicli.com/docs/resources/quota-and-pricing/).

To move work from Codex, first create or select a confirmed Relay Markdown
handoff. Open the Gemini CLI in the project and explicitly ask it to read that
file, reconcile current files and avoid repeating completed actions. This sends
the file to Google when you submit the prompt. No automatic handoff import or
cross-provider rotation is implemented. Native Codex threads, reasoning items
and response references are not portable Gemini sessions.

## Scope and validation

- Supported: separate native homes, profile aliases, archive/restore, sign-in
  and working-session launch commands, dashboard metadata controls.
- Not implemented: Gemini inference inside the Codex proxy, automatic Google
  quota polling, automatic account rotation or cross-provider handoff import.
- Offline HTML displays a server-required notice and does not manage profiles.
- Google sign-in, real model requests and multiple-account isolation with an
  installed CLI have not been integration-tested. Unit tests use temporary
  homes and mocked processes, with no real login or inference.

Runtime state lives in the ignored `.relay-accounts/gemini/` directory. Native
credential/session storage may contain private data and is not encrypted by
Relay. Tokens and native session files are never included in dashboard responses
or Codex account backup exports.
