# Host-Exec Injection Regression Test Plan

Status: implemented. The general suite lives in
`packages/host-exec-daemon/tests/test_host_exec_injection.py`, with focused
regression modules for the fixed gaps
(`test_host_exec_arg_boundaries.py`, `test_host_exec_workspaces.py`,
`test_host_exec_approval_fallback.py`, `test_host_exec_bind.py`).

This document defines what the suite protects, the threat model, the full
vector matrix, and the status of the gaps found by adversarial review.

## 1. The property under test

> A whitelisted host command can never cause an attacker-controlled secondary
> command to run, even when the caller fully controls the command name, the
> argument vector, the working directory, and every extra JSON field.

The classic failure mode is a whitelist implemented as a regular expression
over a command *string*: a policy that accepts `sw_vers` also accepts
`sw_vers && rm -rf /` because `&&` matches. This bridge avoids that class of
bug structurally rather than by escaping, and the suite exists to keep that
structure from regressing.

## 2. Threat model

| | |
|---|---|
| **Attacker** | Code inside the sandbox container (the agent itself). It can read the shared HMAC secret, so it can craft arbitrary well-signed requests. |
| **Attacker cannot** | Modify the daemon's Python, read the host filesystem directly, or reach the host except through the bridge. |
| **Goal** | Get the host to execute a binary or argument that the whitelist did not intend to allow. |
| **In scope** | Command-name smuggling, shell metacharacters in args, regex/fullmatch bypass, HMAC canonicalization gaps, type confusion, cwd abuse, approval-dialog injection, DoS on the daemon. |
| **Out of scope** | Host compromise, macOS TCC/sandbox escapes, a malicious `binary_path` written by the *user* into `whitelist.yaml`, and network attackers without the secret (see GAP-4). |

## 3. Trust boundary

```
guest/bin/host-exec            (container)  builds JSON {command, args[], cwd, token}
        │  TCP, HMAC-signed
        ▼
HostExecServer.handle_client   (host)       verify → lookup → regex → approve → spawn
        │  create_subprocess_exec([binary_path] + args)
        ▼
whitelisted host binary        (host)       argv vector, no shell
```

## 4. Defense layers and the tests that pin them

### L1 — Authentication (`verify_token`, `handle_client`)
The HMAC-SHA256 signature covers `(command, args, cwd)` in canonical JSON and
is checked **before** the command is dispatched or any policy is evaluated,
including the `__list__` query. It is *not* checked before `load_whitelist()`
(plain file I/O), so the security claim is "verified before dispatch", not
"before any work at all".

Tests (`TestHMACAuthentication`, `TestHMACCanonicalization`):
- missing / empty / non-hex / wrong-length tokens rejected;
- tampered `args` after signing rejected;
- tokens are not transferable across command or cwd;
- `__list__` requires authentication;
- extra JSON fields cannot redirect the spawn (asserted on the spawn vector);
- **sign-what-you-execute**: a token computed over the coerced form is rejected
  when the wire value differs (e.g. list-signed token with a string `args`);
- `args: null` is signed and coerced consistently to an empty argv;
- duplicate JSON keys use the same parse for signing and dispatch (no differential).

### L2 — Naming (`handle_client`: `if command_name not in policies`)
The command is an **exact dict-key lookup**, never a regex and never a shell
word. `str()` coercion of a non-string command cannot turn a list into a key.

Tests (`TestCommandNameIsExactKeyLookup`):
- `echo_any && touch …`, `echo_any; …`, `echo_any|…`, embedded newlines,
  backticks, `$(…)`, leading/trailing whitespace, path traversal, `sh`,
  `/bin/sh` — all rejected with `not_whitelisted` and nothing executed;
- case sensitivity;
- JSON type confusion (`command` as a list) rejected;
- aliases resolve to the configured `binary_path`, not to argv[0].

### L3 — Argument policy (`re.fullmatch(regex, shlex.join(args))`)
The regex is applied with **`re.fullmatch`** (anchored) and only to the
argument string. Wildcard authors are warned by the trailing-anchor control
test below.

Tests (`TestArgumentPolicyFullMatch`):
- a strict pattern rejects prefixes, suffixes, extra elements, trailing
  whitespace, and trailing newlines;
- args matching no pattern yield `args_violation` and never execute;
- empty vector matches an `^$` policy;
- a plain `^.*$` (no DOTALL) rejects multiline args;
- an explicitly anchored flag pattern rejects extra flags (control for GAP-1).

### L4 — Execution (`cmd = [bin_path] + args; create_subprocess_exec(*cmd)`)
There is no shell: `&&`, `;`, `|`, backticks, `$(…)`, `$IFS`, redirects,
newlines, NUL bytes, and unicode are all inert literal argv elements.

Tests (`TestArgumentVectorIsInert`, `TestExecutionUsesArgvNotShell`):
- real subprocesses: a Python argv probe echoes back the exact vector it
  received, and a marker file a real shell would have created is never present;
- unit: the mocked spawn is exactly `(binary_path, *args)` with no
  `shell`/`env`/`executable` keyword;
- source guard: no `create_subprocess_shell`, `shell=True`, `os.system`,
  `os.popen`, `subprocess.*`, `pty.spawn`, `os.spawn`, or AppleScript
  `do shell script`.

### Cross-cutting — input coercion, transport, resilience
Tests (`TestInputCoercion`, `TestServerResilience`):
- non-list `args` (string / dict) coerce to an empty vector, never a parsed
  command line;
- nested list args are stringified, not flattened;
- a bogus `cwd` cannot inject and degrades to the host home;
- malformed JSON, non-UTF-8 bytes, NUL bytes, and oversize request lines all
  fail cleanly and the server stays healthy;
- unicode / bidi / emoji args are passed through literally.

### Approval dialog
Tests (`TestApprovalGate`, `TestApprovalPrimaryPathArgvSafety`):
- approval receives the original argv and denies/approves correctly;
- overlapping auto/approval patterns require approval;
- the **primary Cocoa dialog is argv-safe**: the AppleScript program is the
  constant `COCOA_DIALOG_SCRIPT`, and user text appears only as inert argv
  elements (`argv[3]`, `argv[4]`), so it can never alter the script.

### Client
Tests (`TestClientNeverBuildsAShellString`):
- `guest/bin/host-exec` transmits `command` and `args` as separate structured
  fields; `&&` stays a list element and is never shell-joined;
- `--list` is structured too;
- the client source contains no subprocess primitives.

## 5. Vector matrix

| Vector | Expected | Test |
|---|---|---|
| `sw_vers && touch X` as args | inert literal arg; X not created | `test_ampersand_is_a_literal_argument` |
| `;`, `\|`, `` ` ``, `$(`, `$IFS`, `<`, `>` in args | inert | `test_all_shell_metacharacter_vectors_are_inert` |
| newline inside an arg | inert | `test_newlines_in_arguments_are_literal` |
| `cmd && touch X` as command name | `not_whitelisted` | `test_command_name_cannot_smuggle_a_second_command` |
| command as JSON list | `not_whitelisted` | `test_command_name_type_confusion_is_rejected` |
| `args` as string / dict | coerced to empty argv | `test_args_as_string_is_coerced_to_empty_vector` |
| token over coerced form, wire differs | auth failure | `test_token_over_list_rejected_when_wire_args_is_string` |
| extra fields `shell/argv/binary_path/env` | spawn vector unchanged | `test_extra_json_fields_cannot_redirect_the_spawn` |
| NUL byte in arg | clean error, server healthy | `test_nul_byte_in_args_returns_clean_error` |
| >64 KiB request line | clean error, server healthy | `test_oversize_request_line_is_rejected_cleanly` |
| duplicate JSON keys | same parse for sign+exec | `test_duplicate_json_keys_use_the_same_parse_for_sign_and_exec` |
| cwd whose name contains `&& touch X` | no injection | `test_cwd_cannot_inject_commands` |
| bogus cwd outside workspace | rejected (`cwd_not_allowed`) | `test_unresolvable_cwd_is_rejected_by_workspace_confinement` |
| AppleScript payload in approval args | program constant | `test_primary_dialog_keeps_user_text_out_of_the_script` |
| trailing `.*` policy + extra flag | rejected (argv boundary) | `test_trailing_wildcard_does_not_authorize_extra_flag` |

## 6. Gap status

The gaps below were found by adversarial review. GAP-1..GAP-4 have been fixed
and each now has a dedicated regression module; GAP-5 and GAP-6 remain open.

### Fixed
- **GAP-1 (High) — argv boundaries.** The policy regex is now matched against a
  newline-joined vector with `re.DOTALL` disabled, so `.` cannot span an
  argument boundary. See `build_args_match_string()` and
  `tests/test_host_exec_arg_boundaries.py`. The shipped `open` and
  `git-credential-osxkeychain` patterns were migrated to consume the boundary
  explicitly.
- **GAP-2 (High) — workspace confinement.** `allowed_workspaces` is enforced on
  the effective working directory with `realpath` containment
  (`path_within_workspaces()`), rejecting with `cwd_not_allowed`. An empty list
  keeps the historical unrestricted behaviour. See
  `tests/test_host_exec_workspaces.py`.
- **GAP-3 (Medium) — fallback dialog.** The fallback is a constant
  `on run argv` program (`FALLBACK_DIALOG_SCRIPT`) and user text is passed as
  argv, never interpolated. See `tests/test_host_exec_approval_fallback.py`.
- **GAP-4 (Low/Medium) — bind exposure.** A wildcard bind is reported loudly at
  startup by `warn_if_bind_is_exposed()`. A loopback default was rejected
  because the sandbox reaches the daemon through `host.docker.internal` (the
  Docker gateway), which a loopback-only listener cannot serve. See
  `tests/test_host_exec_bind.py`.

### Open
- **GAP-5 (Medium) — `binary_path` trusted unchecked.** No validation that it
  is absolute, exists, is executable, and is not a symlink; the approval dialog
  shows the alias rather than the resolved binary; hot-reload can change it
  between approval and execution.
- **GAP-6 (Low) — in-process DoS surfaces.** No application-level request-size
  cap; the approval prompt has no timeout and holds `approval_lock`.

## 7. Invariants to preserve

Any change to the following breaks the security argument and must be caught by
a test here:

1. Command identity is an exact key lookup.
2. Arguments are never joined into a shell string for execution.
3. The process is spawned with `create_subprocess_exec` and an argv vector.
4. The HMAC is verified against the exact value that is executed.
5. Execution happens only after the whitelist **and** (if required) the
   approval gate have passed.

## 8. Running

```bash
python -m pytest packages/host-exec-daemon/tests/   # host-exec suite
python -m pytest packages/                          # full suite
```
