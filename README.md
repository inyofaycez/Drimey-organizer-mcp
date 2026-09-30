# Drime Organizer MCP

A local MCP server that lets an AI browse and safely organize your Drime media storage through rclone's native Drime backend.

**A European chain:** Drime is a French, GDPR-compliant cloud with EU-only data centers; Mistral Vibe is a French AI client; this server runs entirely on your own machine. Storage, AI, and everything in between can stay European.

**Honest origin note:** this server is vibe-coded — designed and written with AI assistants, then audited and hardened. It is not vendor software and has no official connection to Drime. The test suite and the safety model below are the reason that's not as reckless as it sounds.

It exposes exactly seven tools:

| Tool | What it does | Changes Drime? |
|---|---|---|
| `list_folder` | Live folder listing | No |
| `get_info` | Live file/folder metadata | No |
| `refresh_index` | Rebuild the local filename/metadata index | No |
| `search_index` | Search cached names and paths | No |
| `read_text` | Read approved UTF-8 text-like files up to 2 MB | No |
| `plan_organization` | Validate and preview up to 100 folder creations/moves | No |
| `execute_organization_plan` | Execute one approved single-use plan | Yes |

There are no upload, delete, purge, share, sync, mount, arbitrary-command, local-file, or cross-cloud tools.

## Safety model

- The MCP is locked to the one rclone remote supplied in its environment, and every rclone invocation is run with the dedicated configuration named by `RCLONE_CONFIG` (`--config`). The server refuses to start when `RCLONE_CONFIG` is unset, so remotes in the default `~/.config/rclone/rclone.conf` are never exposed. An AI cannot select another remote.
- Use a dedicated rclone config file containing only the Drime remote. Even if a future bug exposed another configured remote, there would be no other cloud credentials in that file.
- Organization is a two-step operation: `plan_organization` checks everything and returns the exact preview plus a random token; `execute_organization_plan` accepts only that token for ten minutes and rechecks the full plan.
- Existing destinations always cause a stop; the MCP's own checks never overwrite or merge files/folders. Narrow race: a file written by another process in the same instant as a move could still be overwritten — background syncs and other tools count as concurrent writers even on single-user machines.
- A batch stops after its first failure. Completed operations are reported and are not rolled back.
- Folder creations and moves are recorded in a local JSONL audit log.
- Paths are relative, normalized, and protected against traversal. Subprocesses never use a shell.

This protects against accidental and AI-initiated misuse through the MCP. The Drime token itself may still have wider account permissions, so protect the dedicated rclone configuration and your macOS account.

## Requirements

- macOS with Python 3.9 or newer (the test suite passes on 3.9; 3.11+ recommended)
- rclone 1.73 or newer (the first release with the native Drime backend)
- A Drime API token from Drime **Settings → Developer**
- Mistral Vibe, or any other client supporting local stdio MCP servers

No Python packages, rclone mount, macFUSE, daemon, open port, or Tailscale connection is required.

**Linux:** nothing in the code is Mac-specific. Only the default file locations assume macOS (`~/Library/...`), so set `DRIME_MCP_INDEX` and `DRIME_MCP_AUDIT` in the server environment (e.g. under `~/.cache/` and `~/.local/state/`) and everything else works the same.

## 1. Create a dedicated rclone configuration

Install rclone if necessary:

```sh
brew install rclone
rclone version
```

Create a configuration containing only this Drime remote:

```sh
mkdir -p "$HOME/.config/rclone"
rclone config --config "$HOME/.config/rclone/drime-mcp.conf"
chmod 600 "$HOME/.config/rclone/drime-mcp.conf"
```

The wizard is interactive. Answer it like this:

1. `n` — new remote
2. `drime-mcp` — the name
3. `drime` — the storage type (type it in, or pick it from the long list)
4. Paste your Drime API token when asked
5. Accept the defaults and confirm with `y`

Then test the connection:

```sh
rclone --config "$HOME/.config/rclone/drime-mcp.conf" lsf drime-mcp:
```

Success looks like a plain list of your top-level folders, one per line. `didn't find section in config file` means the remote name doesn't match; `authorization failed` means the token is wrong or expired.

Keep the token and configuration out of chats, screenshots, public repositories, and client configuration files.

## 2. Install the MCP

Clone this repository to a stable private folder, for example:

```sh
git clone https://github.com/inyofaycez/Drimey-organizer-mcp.git "$HOME/Applications/drimey-organizer-mcp"
chmod 700 "$HOME/Applications/drimey-organizer-mcp/server.py"
```

Replace every `/Users/YOU` and executable path in the examples with the values from your Mac. Find your absolute executable paths now — the client configuration needs them:

```sh
command -v rclone
command -v python3
```

Do not literally use `$HOME` inside client environment values unless that client documents environment expansion; absolute paths are safer.

## 3. Mistral Vibe configuration

Copy the relevant parts of [`examples/vibe-config.toml`](examples/vibe-config.toml) into `~/.vibe/config.toml` and correct the paths.

The example gives automatic permission to the five read-only tools and the preview tool. The only tool that changes Drime, `drime_execute_organization_plan`, is set to `ask`.

Restart Vibe after editing the configuration. Then ask:

> List the root of my Drime using the drime tools. Do not make changes.

Expected: within a few seconds the AI lists your top-level Drime folders. If it reports the server failed to start, the most common cause is a missing `RCLONE_CONFIG` value in the server's env block — see Troubleshooting below.

## First use

1. Ask the AI to run `refresh_index`. A large media library can take several minutes.
2. Search by filename/path with `search_index`, or browse live with `list_folder`.
3. Ask for an organization proposal, for example:

   > Find episodes currently under `Unsorted`, propose suitable folders under `Shows`, and call only `plan_organization`. Show me the complete preview. Do not execute it.

4. Inspect every source and destination in the preview. If correct, explicitly approve that exact preview. Your client should then ask again before permitting `execute_organization_plan`.

The token is single-use and expires after ten minutes. A change to a source's type or size, or a destination appearing, blocks execution and requires a new plan. Same-size content changes to a source are not detected.

## Index and audit files

By default, the MCP uses:

```text
~/Library/Caches/drime-organizer-mcp/index.json
~/Library/Logs/drime-organizer-mcp/audit.jsonl
```

The index contains filenames, paths, file/folder flags, and sizes—not file contents. A successful `execute_organization_plan` rebuilds it from a live Drime listing, so changes made outside this MCP (downloads, direct rclone work) are picked up automatically after each plan; if that rebuild fails, the plan's own moves are patched into the index instead. A partial failure marks the index stale. Otherwise the index is refreshed only when `refresh_index` is called, so refresh before a substantial organization session if the drive changed since the last plan.

The audit log records organization previews only when executed, plus each completed action and failure. The MCP blocks organization if it cannot start the audit log.

## Important behavior

- The 100-item limit counts folder creations and moves together.
- Rename is represented as a move from the old path to the new path.
- The 2 MB content reader accepts only explicitly listed text, subtitle, NFO, playlist, code, and markup extensions. It cannot read media binaries or PDFs.
- A batch is not a transaction. If the network fails after item 17, those 17 changes remain, the MCP stops, the index is marked stale, and the result tells you exactly what completed.
- The Mac needs to be on only while you use this local MCP. Nothing here gives mobile/cloud ChatGPT direct access to it.

## Verification

The included test suite uses a fake rclone process and never connects to Drime. It covers path and Unicode validation, duplicate and overlapping moves, destination conflicts, single-use plan tokens, partial batch failures, expired authentication mid-batch, index refresh and patching, and malformed stdio frames:

```sh
cd "$HOME/Applications/drimey-organizer-mcp"
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py
```

After those tests, verify the real dedicated remote with the read-only `rclone lsf` command shown above before enabling the MCP in your client.

## Troubleshooting

- **Server refuses to start: "RCLONE_CONFIG must point to a dedicated rclone configuration"** — the client environment block is missing `RCLONE_CONFIG`. Add it (pointing at the config file from step 1) and restart the client. This refusal is deliberate: without it, rclone would silently fall back to your default config and expose every remote in it.
- **rclone offers no `drime` storage type, or commands fail with `unknown command`** — your rclone is older than 1.73. Check with `rclone version` and upgrade (`brew upgrade rclone`).
- **`authorization failed` or HTTP 401** — wrong or expired Drime token. Recreate it in Drime **Settings → Developer** and update the config with `rclone config --config "$HOME/.config/rclone/drime-mcp.conf"`.
- **First `refresh_index` seems to hang** — a large library takes minutes for the full listing. The example client configs set a 900-second tool timeout; if yours is shorter, raise it.
- **A plan stops with "destination already exists"** — expected behavior; the MCP refuses to merge or overwrite at plan time. If another process writes to the same destination between preview and execution, that race is outside its control. Pick a different destination or move the conflicting item first.
- **Index is stale after a partial failure** — run `refresh_index` before planning again, as the failure result tells you.

## License and support

MIT — see [LICENSE](LICENSE). Provided as-is with no warranty.

**Disclaimer:** This is an independent, personal project. The author is not affiliated with, associated with, sponsored by, endorsed by, or connected to Drime, its staff, its parent or subsidiary entities, or any of its partners or resellers — in any capacity, whether as an employee, contractor, agent, or representative. There is no business or contractual relationship of any kind between the author and Drime. This software is not an official Drime product; Drime has not reviewed, approved, or supported it. The name "Drime" and any related marks belong to their respective owners and are used here solely to describe interoperability with the service.

It is maintained casually: issues are welcome and will be addressed as time allows, not on a schedule. If it breaks, the test suite, the audit log, and the plan/execute design are there so you can see exactly what happened before anything touched your files.

