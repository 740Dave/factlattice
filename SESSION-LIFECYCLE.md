# FactLattice Session Lifecycle

> How FactLattice manages context loading, engineering logs, and session bookkeeping through manifest-driven hooks in Claude Code.

This document describes the session lifecycle machinery that ensures an Intelligence receives the right context at session start and leaves a clean audit trail at session end. It is framework documentation -- anyone adopting FactLattice for their own domain can follow it to set up context loading from scratch.

> **Design example, not shipped code.** Sections 1-5 specify the behaviour of two hook scripts, `session_start.sh` and `session_end.sh`. Those scripts are NOT part of this repository: everything below describes what you would build and wire up yourself (section 6), and nothing here can be run as-is from a clone.

---

## 1. Session Start -- Manifest-Driven Context Discovery

When Claude Code starts a session, a `SessionStart` hook runs `session_start.sh`. This script performs four operations in sequence:

1. **Engineering log rotation** -- ensures `logs/engineering/` exists with monthly JSONL files
2. **Namespace discovery** -- scans `~/.claude/*/context_manifest.json` to find all registered knowledge namespaces
3. **Directory matching** -- matches the current working directory against each namespace's `match_roots`
4. **Context loading** -- reads the matched namespace's files and emits them to the Intelligence's context window

### Namespace Discovery

A directory under `~/.claude/` is a knowledge namespace if and only if it contains a `context_manifest.json` file. There is no registration step, no blocklist. Drop a directory with a manifest and it participates in discovery automatically.

```
~/.claude/
  myproject/
    context_manifest.json    <-- makes "myproject" a namespace
    00-foundation.lts
    10-architecture.lts
  another-domain/
    context_manifest.json    <-- makes "another-domain" a namespace
    00-domain-model.lts
```

### Directory Matching (Three-Pass Resolution)

The resolver tries three strategies, in order. First match wins.

**Pass 1 -- Explicit `match_roots`.** Each namespace can declare a `match_roots` array of path prefixes. If the working directory equals or is a subdirectory of any entry, the namespace matches.

```json
{
  "match_roots": ["~/myproject", "~/experiments/myproject-staging"]
}
```
Working directory `~/myproject/src/` matches. Working directory `~/other/` does not.

**Pass 2 -- Profile-based matching.** If a namespace uses the `profiles` structure (see Section 2), each profile's `directories` entries are resolved relative to `project_root_base` and checked as path prefixes. This supports organizations with many repositories sharing a common foundation.

**Pass 3 -- Basename matching.** If the working directory's basename matches a namespace name (case-insensitive), the namespace matches. Paths under `$HOME` match authoritatively. Paths outside `$HOME` emit a candidate signal -- the Intelligence is asked whether to permanently register the path in `match_roots`.

### File Loading Order

Context files are emitted to stdout (which Claude Code feeds into the Intelligence's context window) in a deterministic order:

1. **Foundation files** (`required`) -- loaded for every session in this namespace. Organizational identity, system rules, cross-cutting protocols.
2. **Domain files** (`project` or `project_files`) -- loaded based on the matched profile or namespace. Architecture docs, domain models, design decisions.
3. **Personal files** (`symlinks` or additional entries) -- per-user context like notes and preferences.
4. **Device-local files** -- machine-specific context (API keys, local service endpoints) from a `device_local/` directory not checked into version control.
5. **Sub-context files** -- entity-specific files resolved from path variables (see Section 2).

---

## 2. Context Manifest Format

The `context_manifest.json` file is the sole configuration artifact for a namespace. Two formats are supported: **simple** (for most namespaces) and **profile-based** (for organizations with multiple repositories sharing context).

### Simple Manifest

```json
{
  "version": "1.0",
  "description": "Human-readable namespace description",
  "match_roots": ["~/myproject"],
  "project_root": "~/myproject",
  "required": [
    "00-foundation.lts",
    "05-engineering-log-protocol.lts"
  ],
  "project_files": [
    "10-architecture.lts",
    "20-domain-model.lts"
  ]
}
```

| Field | Type | Purpose |
|-------|------|---------|
| `version` | string | Manifest format version (`"1.0"` for simple, `"2.0"` for profile-based) |
| `description` | string | Human-readable namespace description |
| `match_roots` | string[] | Path prefixes that cause this namespace to match. Supports `~` expansion. |
| `project_root` | string | Base path for resolving relative file references. Defaults to the namespace directory. |
| `required` | string[] | Files loaded for every session. Foundation and protocol docs. |
| `project_files` | string[] or object | Domain-specific files. If an object, keys are filenames and values are metadata (description, tier). |
| `reference_docs` | object | Named paths to reference material available on demand but not auto-loaded. |

#### File Resolution

When loading a file reference like `"10-architecture.lts"`, the resolver checks two locations in order:
1. The namespace config directory (`~/.claude/<namespace>/10-architecture.lts`)
2. The manifest's `project_root` (`~/myproject/10-architecture.lts`)

First existing path wins.

### Profile-Based Manifest

For organizations where multiple projects share a common foundation but need different domain files:

```json
{
  "version": "2.0",
  "context_dir": "~/.claude/myorg",
  "project_root_base": "~",
  "local_profiles": "~/.claude/myorg_local/context_manifest_local.json",
  "profiles": {
    "backend": {
      "description": "Backend service development",
      "directories": ["backend", "backend-staging"],
      "required": [
        "00-org-foundation.lts",
        "01-system-rules.lts",
        "05-engineering-log-protocol.lts"
      ],
      "project": [
        "backend/10-architecture.lts",
        "backend/20-api-model.lts"
      ],
      "symlinks": ["97-claude_notes.jsonl", "98-user_context.jsonl"]
    },
    "frontend": {
      "description": "Frontend application",
      "directories": ["frontend", "frontend-v2"],
      "required": [
        "00-org-foundation.lts",
        "01-system-rules.lts"
      ],
      "project": [
        "frontend/10-component-architecture.lts"
      ],
      "symlinks": ["97-claude_notes.jsonl", "98-user_context.jsonl"]
    },
    "default": {
      "description": "Fallback for unrecognized project directories",
      "directories": [],
      "required": [
        "00-org-foundation.lts",
        "01-system-rules.lts"
      ],
      "project": [],
      "symlinks": ["97-claude_notes.jsonl", "98-user_context.jsonl"]
    }
  },
  "files": {
    "00-org-foundation.lts": { "description": "Organization identity and principles", "tier": "foundation" },
    "01-system-rules.lts": { "description": "Context system rules", "tier": "meta" },
    "backend/10-architecture.lts": { "description": "Backend service architecture", "tier": "architecture" }
  }
}
```

| Field | Type | Purpose |
|-------|------|---------|
| `project_root_base` | string | Base path. Profile `directories` entries are resolved as `{project_root_base}/{directory}`. |
| `local_profiles` | string | Path to a machine-local profiles file, merged at load time. Not checked into version control. |
| `profiles.<name>.directories` | string[] | Directory names (relative to `project_root_base`) that trigger this profile. |
| `profiles.<name>.project_roots` | string[] | Absolute paths that trigger this profile (alternative to `directories`). |
| `profiles.<name>.required` | string[] | Foundation files, loaded first. |
| `profiles.<name>.project` | string[] | Domain files, loaded after required. |
| `profiles.<name>.symlinks` | string[] | Personal context files, loaded last. |
| `profiles.default` | object | Fallback profile when no directory matches. |
| `files` | object | Optional metadata catalog. Keys are filenames, values describe each file's purpose and tier. Not used for loading -- purely documentary. |

### Sub-Context Extraction

For namespaces where the working directory path encodes entity identity (e.g., patient records, client projects), the `sub_context` block extracts path variables and loads entity-specific files:

```json
{
  "match_roots": ["~/clients"],
  "required": ["00-consulting-foundation.lts"],
  "sub_context": {
    "match_pattern": "accounts/{client}",
    "context_path": "accounts/{client}/"
  }
}
```

If the working directory is `~/clients/accounts/acme-corp/`, the resolver:
1. Extracts `{client}` = `acme-corp`
2. Looks for `~/.claude/<namespace>/accounts/acme-corp/`
3. Loads all `.lts` and `.md` files from that directory, sorted alphabetically

This allows per-entity context files to be maintained alongside the namespace without modifying the manifest for each new entity.

---

## 3. Hook Configuration

FactLattice session lifecycle is driven by Claude Code's hook system, configured in `~/.claude/settings.json`.

### settings.json Format

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/scripts/session_start.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/scripts/session_end.sh"
          }
        ]
      }
    ]
  }
}
```

| Hook | Matcher | When It Fires |
|------|---------|---------------|
| `SessionStart` | `"startup"` | Once at the beginning of every Claude Code session |
| `SessionEnd` | `""` (empty = always) | Once when the session is ending |

### How Hooks Communicate

Both hooks run as shell commands. Their **stdout** is fed into the Intelligence's context window as part of the session. This is the sole communication channel -- hooks cannot prompt interactively.

The `session_start.sh` hook emits:
- **File contents** -- each context file is `cat`'d to stdout in loading order
- **JSON signals** -- structured messages for the Intelligence to act on:
  - `{"type": "session_end_warning", ...}` -- previous session ended uncleanly
  - `{"type": "namespace_candidate", ...}` -- directory name matched a namespace but path is not registered
  - `{"type": "error", ...}` -- manifest parsing or file loading failure
  - `{"type": "memory_bootstrap", ...}` -- instructions for loading personal memory via MCP

The `session_end.sh` hook emits:
  - `{"type": "session_end_warning", ...}` -- alert if no engineering log entries were written during the session

### Session Canary

At session start, a canary file is planted at `<project>/.claude/session_canary.json`:

```json
{
  "session_start": "2026-03-28T16:00:00Z",
  "profile": "backend",
  "log_file": "/path/to/logs/engineering/2026-03.jsonl",
  "lines_at_start": 42
}
```

At session end, the canary is read to determine:
- Whether the Intelligence wrote any engineering log entries (by comparing line counts)
- How long the session lasted
- Which profile was active

The canary is deleted after processing. An orphaned canary at session start indicates an unclean prior shutdown, and a warning is emitted.

---

## 4. Engineering Log Protocol

FactLattice uses JSONL (JSON Lines) files for engineering logs -- one file per calendar month, stored in `<project>/logs/engineering/`.

### Directory Structure

```
myproject/
  logs/
    engineering/
      2026-01.jsonl
      2026-02.jsonl
      2026-03.jsonl
      latest.jsonl -> 2026-03.jsonl    # symlink to current month
```

The `latest.jsonl` symlink is updated at each session start. Monthly rotation is automatic -- the hook creates new files as needed.

### Entry Types

Each line is a self-contained JSON object. Common entry types:

```jsonl
{"type":"session","source":"hook","id":"session-2026-03-28a1b2","date":"2026-03-28T16:00:00Z","status":"active","profile":"backend","project_root":"/path/to/project"}
{"type":"discovery","source":"hook","id":"disc-gap-20260328160000","date":"2026-03-28T16:00:00Z","session":"session-2026-03-28a1b2","description":"Time gap detected: 72.3h between last log entry and most recent project file modification.","severity":"warning"}
{"type":"engineering","source":"claude","date":"2026-03-28T16:15:00Z","session":"session-2026-03-28a1b2","description":"Refactored auth middleware to support token rotation.","decisions":["Chose JWT refresh over session cookies for statelessness"],"files_modified":["src/auth/middleware.py"]}
{"type":"session","source":"hook","id":"session-end-20260328170000","date":"2026-03-28T17:00:00Z","status":"ended","session_start":"2026-03-28T16:00:00Z","profile":"backend","claude_entries_detected":true,"claude_entry_count":3}
```

| Type | Source | Purpose |
|------|--------|---------|
| `session` (status: active) | hook | Session start bookend. Written automatically. |
| `session` (status: ended) | hook | Session end bookend. Written automatically. |
| `discovery` | hook | Gap warnings, anomalies detected by the hook. |
| `engineering` | claude | Implementation decisions, rationale, work done. Written by the Intelligence during the session. |
| Any custom type | claude | Domain-specific entries. The schema is open -- use whatever types serve your project. |

### Session Bookending

Every session is bracketed by two hook-written entries:
1. **Session start** -- records timestamp, profile, project root, and a unique session ID
2. **Session end** -- records duration, whether the Intelligence wrote any entries, and entry counts

The `source` field distinguishes hook-written entries (`"hook"`) from Intelligence-written entries (`"claude"` or omitted).

### Time Gap Detection

At session start, the hook compares:
- The timestamp of the most recent engineering log entry
- The mtime of the most recently modified project file

If the gap exceeds 24 hours, a `discovery` entry is written warning that work may have occurred without logging. This catches sessions where the Intelligence forgot to log, or where someone edited files outside a Claude Code session.

### Legacy Migration

The hook automatically migrates older log formats:
- Flat files (`engineering_log.jsonl` in the project root) are split into monthly files
- Entries are bucketed by their `date` field into the correct month file
- Legacy files are removed after successful migration

---

## 5. Session End

The `session_end.sh` hook performs three checks:

### 1. Project Detection

If the working directory does not look like a project (no `src/`, `pyproject.toml`, `.git`, or `.claude/` directory), the hook exits silently. Engineering logs are a project-level concern.

### 2. Log Completeness Check

The hook reads the session canary and counts engineering log entries written since session start:

- **Hook entries** -- the session-start bookend and any gap warnings (source: `"hook"`)
- **Intelligence entries** -- everything else

If the Intelligence wrote zero entries, the hook emits a high-priority warning to stdout. Because this happens during session teardown, the Intelligence has a narrow window to write best-effort entries before the session fully closes.

### 3. Cleanup

The session canary file is deleted. If it was already missing (e.g., the hook was never planted), the end hook exits gracefully.

---

## 6. Installation

> **This is a design example.** `session_start.sh` and `session_end.sh` are NOT part of this repository. This section describes what you would wire up if you write those two hook scripts yourself, following the behaviour specified in sections 2-5. Nothing below can be run as-is from a clone.

### Prerequisites

- Claude Code installed and functional
- Python 3 available on `$PATH` (used by the hook scripts for JSON parsing and log analysis)
- A `~/.claude/` directory (created by Claude Code on first run)

### Step 1: Create the Scripts Directory

Write your own `session_start.sh` and `session_end.sh` (they are not shipped here) and place them in `~/.claude/scripts/`:

```bash
mkdir -p ~/.claude/scripts
# put your own session_start.sh and session_end.sh here
```

### Step 2: Configure Hooks

Create or update `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/scripts/session_start.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/scripts/session_end.sh"
          }
        ]
      }
    ]
  }
}
```

If `settings.json` already exists with other configuration, merge the `hooks` key -- do not overwrite the file.

### Step 3: Create Your First Namespace

```bash
mkdir -p ~/.claude/myproject
```

Write `~/.claude/myproject/context_manifest.json`:

```json
{
  "version": "1.0",
  "description": "My project namespace",
  "match_roots": ["~/myproject"],
  "required": [
    "00-foundation.lts"
  ],
  "project_files": [
    "10-architecture.lts"
  ]
}
```

Place your FactLattice files (or Markdown, or any text) in `~/.claude/myproject/`. The hook will load them at session start whenever you open Claude Code in `~/myproject/` or any subdirectory.

### Step 4: Set Up Engineering Logs

Create the log directory in your project:

```bash
mkdir -p ~/myproject/logs/engineering
```

The hook will create monthly files and the `latest.jsonl` symlink automatically. You can also let the hook create the directory -- it will do so on first session start if the project root is detected.

### Step 5: Verify

Start a Claude Code session in your project directory. Check `~/.claude/hook_run.log` for diagnostics:

```bash
tail -20 ~/.claude/hook_run.log
```

You should see lines like:
```
2026-03-28 16:00:00 - session_start.sh executed from /home/you/myproject
2026-03-28 16:00:00 - Namespace resolved: myproject for /home/you/myproject
2026-03-28 16:00:00 - [myproject] Loaded: 00-foundation.lts
2026-03-28 16:00:00 - [myproject] Loaded: 10-architecture.lts
2026-03-28 16:00:00 - Loaded 2 context files for namespace 'myproject'
```

### Adding a Second Namespace

Namespaces are independent. Add another directory with a manifest and it participates in discovery immediately:

```bash
mkdir -p ~/.claude/sideproject
cat > ~/.claude/sideproject/context_manifest.json << 'EOF'
{
  "version": "1.0",
  "description": "Side project context",
  "match_roots": ["~/sideproject"],
  "required": ["00-overview.lts"]
}
EOF
```

No changes to `session_start.sh` or `settings.json` required. The discovery scan finds it automatically.

### Local Profiles (Machine-Specific Overrides)

For profile-based namespaces, create a local profiles file that is not checked into version control:

```bash
mkdir -p ~/.claude/myorg_local
cat > ~/.claude/myorg_local/context_manifest_local.json << 'EOF'
{
  "profiles": {
    "local-experiment": {
      "description": "Local experiment not in shared manifest",
      "directories": ["experiment-x"],
      "required": ["00-org-foundation.lts"],
      "project": [],
      "symlinks": []
    }
  }
}
EOF
```

Reference it from the main manifest via the `local_profiles` field. Profiles from the local file are merged with shared profiles at load time.
