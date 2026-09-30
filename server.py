#!/usr/bin/env python3
"""Dependency-free, local Drime organizer MCP server over stdio."""

from __future__ import annotations

import json
import os
import secrets
import selectors
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

VERSION = "0.2.0"
PROTOCOLS = {"2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"}
MAX_TEXT_BYTES = 2_000_000
MAX_REQUEST_BYTES = 1_000_000
MAX_LIST_ENTRIES = 500
MAX_BATCH_OPERATIONS = 100
MAX_INDEX_BYTES = 64_000_000
MAX_INDEX_ENTRIES = 500_000
PLAN_TTL_SECONDS = 600
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".toml", ".log",
    ".nfo", ".srt", ".vtt", ".ass", ".ssa", ".sub", ".m3u", ".m3u8",
    ".pls", ".cue", ".r", ".py", ".js", ".ts", ".sh", ".sql",
    ".tex", ".bib",
}
PLANS: dict[str, dict] = {}


class RcloneError(ValueError):
    def __init__(self, message: str, returncode: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_index_path() -> Path:
    return Path(os.environ.get(
        "DRIME_MCP_INDEX",
        str(Path.home() / "Library/Caches/drime-organizer-mcp/index.json"),
    )).expanduser()


def default_audit_path() -> Path:
    return Path(os.environ.get(
        "DRIME_MCP_AUDIT",
        str(Path.home() / "Library/Logs/drime-organizer-mcp/audit.jsonl"),
    )).expanduser()


def safe_path(value: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError("Path must be text")
    value = unicodedata.normalize("NFC", value)
    if len(value) > 4096 or value.startswith("/") or "\\" in value:
        raise ValueError("Use a relative path inside the configured Drime remote")
    # Reject control (Cc), format (Cf), surrogate (Cs), and line/paragraph
    # separator (Zl/Zp) characters, which can visually disguise a path in the
    # approval preview. ZWNJ (U+200C) and ZWJ (U+200D) stay allowed because they
    # occur in legitimate Arabic, Persian, and Indic filenames.
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        and char not in ("\u200c", "\u200d")
        for char in value
    ):
        raise ValueError("Path contains control, invisible, or bidirectional characters")
    if not value:
        if allow_empty:
            return ""
        raise ValueError("Choose a non-root path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Path traversal and empty path components are not allowed")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ValueError("A path component is longer than 255 bytes")
    return value


def rclone_config() -> str:
    value = os.environ.get("RCLONE_CONFIG", "")
    if not value:
        raise ValueError("RCLONE_CONFIG must point to a dedicated rclone configuration")
    return value


def remote_root() -> str:
    root = os.environ.get("DRIME_MCP_REMOTE", "drime-mcp:")
    if ":" not in root or root.startswith(":") or root.endswith("/") or "\\" in root:
        raise ValueError("DRIME_MCP_REMOTE must look like drime-mcp: or drime-mcp:Folder")
    name, suffix = root.split(":", 1)
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid rclone remote name")
    safe_path(suffix)
    return root


def remote_path(path: str) -> str:
    root = remote_root()
    clean = safe_path(path)
    if not clean:
        return root
    return root + ("" if root.endswith(":") else "/") + clean


def run_rclone(args: list[str], *, maximum: int = 4_000_000, timeout: int = 90) -> bytes:
    binary = os.environ.get("RCLONE_BIN", "rclone")
    process = subprocess.Popen(
        [binary, "--config", rclone_config(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = bytearray()
    errors = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RcloneError("Drime operation timed out")
            ready = selector.select(remaining)
            if not ready:
                raise RcloneError("Drime operation timed out")
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise RcloneError("Drime response exceeds the configured safety limit")
                elif len(errors) < 131_072:
                    errors.extend(chunk[: 131_072 - len(errors)])
        remaining = max(0.1, deadline - time.monotonic())
        process.wait(timeout=remaining)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    stderr = errors.decode("utf-8", errors="replace").strip()
    if process.returncode:
        raise RcloneError(
            stderr or "rclone could not complete the Drime operation",
            process.returncode,
            stderr,
        )
    return bytes(output)


def is_missing(error: RcloneError) -> bool:
    message = error.stderr.casefold()
    return error.returncode == 3 or "not found" in message or "directory not found" in message


def stat_any(path: str, *, missing_ok: bool = False) -> dict | None:
    clean = safe_path(path)
    if not clean:
        return {"Path": "", "Name": "", "IsDir": True, "Size": -1}
    try:
        raw = run_rclone([
            "lsjson", remote_path(clean), "--stat", "--no-modtime", "--no-mimetype",
        ])
    except RcloneError as error:
        if missing_ok and is_missing(error):
            return None
        raise ValueError(f"Could not inspect {clean}: {error}") from None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Unexpected metadata response from Drime")
    value["Path"] = clean
    return value


def list_folder(arguments: dict) -> dict:
    folder = safe_path(arguments.get("path", ""))
    limit = bounded_int(arguments.get("limit", 100), 1, MAX_LIST_ENTRIES, "limit")
    raw = run_rclone([
        "lsjson", remote_path(folder), "--no-modtime", "--no-mimetype",
    ], maximum=8_000_000)
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError("Unexpected folder response from Drime")
    rows = []
    skipped = 0
    for entry in entries[:limit]:
        name = entry.get("Path")
        if not isinstance(name, str):
            skipped += 1
            continue
        try:
            full = safe_path(f"{folder}/{name}" if folder else name)
        except ValueError:
            skipped += 1
            continue
        rows.append({"path": full, "is_dir": bool(entry.get("IsDir")), "size": entry.get("Size")})
    return {
        "folder": folder or "/",
        "entries": rows,
        "total_in_folder": len(entries),
        "truncated": len(entries) > limit,
        "skipped_entries": skipped,
    }


def get_info(arguments: dict) -> dict:
    path = safe_path(arguments.get("path", ""), allow_empty=False)
    entry = stat_any(path)
    return {"path": path, "is_dir": bool(entry.get("IsDir")), "size": entry.get("Size")}


def bounded_int(value, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_index() -> dict:
    path = default_index_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("No index exists yet. Run refresh_index first") from None
    except (OSError, json.JSONDecodeError):
        raise ValueError("The local index cannot be read. Run refresh_index to replace it") from None
    if value.get("remote") != remote_root() or not isinstance(value.get("entries"), list):
        raise ValueError("The index belongs to another remote or is invalid. Run refresh_index")
    for entry in value["entries"]:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("is_dir"), bool)):
            raise ValueError("The index belongs to another remote or is invalid. Run refresh_index")
    return value


def refresh_index(arguments: dict) -> dict:
    del arguments
    raw = run_rclone([
        "lsjson", remote_path(""), "--recursive", "--no-modtime", "--no-mimetype",
    ], maximum=MAX_INDEX_BYTES, timeout=900)
    source = json.loads(raw)
    if not isinstance(source, list):
        raise ValueError("Unexpected recursive listing response from Drime")
    if len(source) > MAX_INDEX_ENTRIES:
        raise ValueError(f"The drive contains more than {MAX_INDEX_ENTRIES:,} indexable entries")
    entries = []
    skipped = 0
    for entry in source:
        path = entry.get("Path")
        if not isinstance(path, str):
            skipped += 1
            continue
        try:
            path = safe_path(path, allow_empty=False)
        except ValueError:
            skipped += 1
            continue
        entries.append({"path": path, "is_dir": bool(entry.get("IsDir")), "size": entry.get("Size")})
    value = {
        "version": 1,
        "remote": remote_root(),
        "refreshed_at": now_iso(),
        "patched_at": None,
        "stale": False,
        "entries": entries,
    }
    atomic_write_json(default_index_path(), value)
    return {
        "indexed_entries": len(entries),
        "refreshed_at": value["refreshed_at"],
        "stale": False,
        "skipped_entries": skipped,
    }


def search_index(arguments: dict) -> dict:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("query must contain 1–200 characters")
    if any(ord(char) < 32 for char in query):
        raise ValueError("query contains control characters")
    kind = arguments.get("kind", "any")
    if kind not in {"any", "file", "folder"}:
        raise ValueError("kind must be any, file, or folder")
    limit = bounded_int(arguments.get("limit", 50), 1, 100, "limit")
    terms = [part.casefold() for part in query.split()]
    index = load_index()
    matches = []
    for entry in index["entries"]:
        if kind == "file" and entry["is_dir"]:
            continue
        if kind == "folder" and not entry["is_dir"]:
            continue
        haystack = entry["path"].casefold()
        if all(term in haystack for term in terms):
            matches.append(entry)
    matches.sort(key=lambda row: (PurePosixPath(row["path"]).name.casefold(), row["path"].casefold()))
    return {
        "matches": matches[:limit],
        "total_matches": len(matches),
        "truncated": len(matches) > limit,
        "refreshed_at": index.get("refreshed_at"),
        "patched_at": index.get("patched_at"),
        "stale": bool(index.get("stale")),
        "note": "This searches filenames and paths only, not file contents.",
    }


def read_text(arguments: dict) -> dict:
    path = safe_path(arguments.get("path", ""), allow_empty=False)
    if PurePosixPath(path).suffix.casefold() not in TEXT_EXTENSIONS:
        raise ValueError("This tool only reads approved text, subtitle, NFO, and playlist extensions")
    entry = stat_any(path)
    if entry.get("IsDir"):
        raise ValueError("Choose a file rather than a folder")
    size = entry.get("Size")
    if not isinstance(size, int) or size < 0 or size > MAX_TEXT_BYTES:
        raise ValueError(f"Text file exceeds the {MAX_TEXT_BYTES:,}-byte limit or has unknown size")
    data = run_rclone(["cat", remote_path(path), "--count", str(size + 1)], maximum=MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"Text file grew beyond the {MAX_TEXT_BYTES:,}-byte limit")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("File is not UTF-8 text") from None
    return {"path": path, "content": content, "source_bytes": len(data)}


def parent_path(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def path_contains(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent + "/")


def normalize_plan(arguments: dict) -> dict:
    folders_raw = arguments.get("create_folders", [])
    moves_raw = arguments.get("moves", [])
    if not isinstance(folders_raw, list) or not isinstance(moves_raw, list):
        raise ValueError("create_folders and moves must be arrays")
    if not folders_raw and not moves_raw:
        raise ValueError("The plan must contain at least one folder creation or move")
    if len(folders_raw) + len(moves_raw) > MAX_BATCH_OPERATIONS:
        raise ValueError(f"A plan may contain at most {MAX_BATCH_OPERATIONS} operations")
    folders = [safe_path(path, allow_empty=False) for path in folders_raw]
    if len(set(folders)) != len(folders):
        raise ValueError("Folder creation paths must be unique")
    moves = []
    for raw in moves_raw:
        if not isinstance(raw, dict):
            raise ValueError("Each move must contain source and destination")
        source = safe_path(raw.get("source", ""), allow_empty=False)
        destination = safe_path(raw.get("destination", ""), allow_empty=False)
        if source == destination:
            raise ValueError(f"Source and destination are identical: {source}")
        if path_contains(source, destination):
            raise ValueError(f"A destination cannot be inside its source: {source}")
        moves.append({"source": source, "destination": destination})
    sources = [move["source"] for move in moves]
    destinations = [move["destination"] for move in moves]
    if len(set(sources)) != len(sources) or len(set(destinations)) != len(destinations):
        raise ValueError("Move sources and destinations must each be unique")
    for index, first in enumerate(sources):
        for second in sources[index + 1:]:
            if path_contains(first, second) or path_contains(second, first):
                raise ValueError("Move sources may not overlap")
    for index, first in enumerate(destinations):
        for second in destinations[index + 1:]:
            if path_contains(first, second) or path_contains(second, first):
                raise ValueError("Move destinations may not overlap")
    if set(sources) & set(destinations):
        raise ValueError("A destination may not also be another move source in the same plan")
    if set(folders) & set(destinations):
        raise ValueError("A folder creation path may not also be a move destination")
    return {"create_folders": folders, "moves": moves}


def preflight(plan: dict, expected_sources: dict[str, dict] | None = None) -> dict[str, dict]:
    planned_folders = set(plan["create_folders"])
    for folder in sorted(planned_folders, key=lambda item: (item.count("/"), item)):
        if stat_any(folder, missing_ok=True) is not None:
            raise ValueError(f"Folder creation conflict; path already exists: {folder}")
        parent = parent_path(folder)
        if parent and parent not in planned_folders:
            parent_entry = stat_any(parent, missing_ok=True)
            if parent_entry is None or not parent_entry.get("IsDir"):
                raise ValueError(f"Parent folder does not exist: {parent}")
    snapshots = {}
    for move in plan["moves"]:
        source = stat_any(move["source"], missing_ok=True)
        if source is None:
            raise ValueError(f"Move source does not exist: {move['source']}")
        if stat_any(move["destination"], missing_ok=True) is not None:
            raise ValueError(f"Move conflict; destination already exists: {move['destination']}")
        parent = parent_path(move["destination"])
        if parent and parent not in planned_folders:
            parent_entry = stat_any(parent, missing_ok=True)
            if parent_entry is None or not parent_entry.get("IsDir"):
                raise ValueError(f"Destination parent folder does not exist: {parent}")
        snapshot = {"is_dir": bool(source.get("IsDir")), "size": source.get("Size")}
        if expected_sources is not None and snapshot != expected_sources.get(move["source"]):
            raise ValueError(f"Move source changed after preview: {move['source']}")
        snapshots[move["source"]] = snapshot
    return snapshots


def purge_expired_plans() -> None:
    current = time.time()
    for token in list(PLANS):
        if PLANS[token]["expires_at_epoch"] <= current:
            del PLANS[token]


def plan_organization(arguments: dict) -> dict:
    purge_expired_plans()
    plan = normalize_plan(arguments)
    snapshots = preflight(plan)
    token = secrets.token_urlsafe(24)
    expires_epoch = time.time() + PLAN_TTL_SECONDS
    PLANS[token] = {
        "plan": plan,
        "snapshots": snapshots,
        "expires_at_epoch": expires_epoch,
    }
    operations = ([{"action": "create_folder", "path": path} for path in plan["create_folders"]]
                  + [{"action": "move", **move} for move in plan["moves"]])
    return {
        "status": "preview_only",
        "plan_token": token,
        "expires_at": datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat().replace("+00:00", "Z"),
        "operation_count": len(operations),
        "operations": operations,
        "warning": "Nothing has changed. Approval must call execute_organization_plan with this exact single-use token.",
    }


def append_audit(event: str, details: dict, *, required: bool = True) -> None:
    path = default_audit_path()
    record = json.dumps({"time": now_iso(), "event": event, **details}, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            buffer = memoryview(record.encode("utf-8"))
            while buffer:
                written = os.write(descriptor, buffer)
                if not written:
                    raise OSError("audit write made no progress")
                buffer = buffer[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        if required:
            raise ValueError(f"Cannot write the local audit log; organization was blocked: {error}") from None


def patch_index(plan: dict, *, stale: bool, completed: list[dict] | None = None) -> None:
    try:
        index = load_index()
    except ValueError:
        return
    entries = index["entries"]
    paths = {entry["path"] for entry in entries}
    completed_folders = ({item["path"] for item in completed if item["action"] == "create_folder"}
                         if completed is not None else set(plan["create_folders"]))
    completed_moves = ([item for item in completed if item["action"] == "move"]
                       if completed is not None else plan["moves"])
    for folder in plan["create_folders"]:
        if folder not in completed_folders:
            continue
        if folder not in paths:
            entries.append({"path": folder, "is_dir": True, "size": -1})
            paths.add(folder)
    for move in completed_moves:
        source = move["source"]
        destination = move["destination"]
        for entry in entries:
            if entry["path"] == source or entry["path"].startswith(source + "/"):
                entry["path"] = destination + entry["path"][len(source):]
    index["entries"] = entries
    index["patched_at"] = now_iso()
    index["stale"] = stale
    try:
        atomic_write_json(default_index_path(), index)
    except OSError:
        pass


def refresh_index_after_plan() -> bool:
    try:
        refresh_index({})
    except (ValueError, OSError, RcloneError, subprocess.SubprocessError):
        return False
    return True


def execute_organization_plan(arguments: dict) -> dict:
    purge_expired_plans()
    token = arguments.get("plan_token")
    if not isinstance(token, str) or not token:
        raise ValueError("A plan_token from plan_organization is required")
    record = PLANS.pop(token, None)
    if record is None:
        raise ValueError("Plan token is invalid, expired, or already used")
    plan = record["plan"]
    try:
        preflight(plan, record["snapshots"])
    except ValueError as error:
        append_audit("plan_rejected", {
            "plan_token": token,
            "plan": plan,
            "error": str(error),
        }, required=False)
        raise
    append_audit("plan_started", {"plan_token": token, "plan": plan})
    completed = []
    current = None
    try:
        for folder in sorted(plan["create_folders"], key=lambda item: (item.count("/"), item)):
            current = {"action": "create_folder", "path": folder}
            run_rclone(["mkdir", remote_path(folder)], maximum=100_000)
            created = stat_any(folder, missing_ok=True)
            if created is None or not created.get("IsDir"):
                raise ValueError(f"Folder creation could not be verified: {folder}")
            completed.append(current)
            append_audit("operation_completed", {"plan_token": token, "operation": current})
        for move in plan["moves"]:
            current = {"action": "move", **move}
            source = stat_any(move["source"], missing_ok=True)
            if source is None:
                raise ValueError(f"Move source disappeared: {move['source']}")
            if stat_any(move["destination"], missing_ok=True) is not None:
                raise ValueError(f"Destination appeared after preview: {move['destination']}")
            snapshot = {"is_dir": bool(source.get("IsDir")), "size": source.get("Size")}
            if snapshot != record["snapshots"][move["source"]]:
                raise ValueError(f"Move source changed after preview: {move['source']}")
            run_rclone([
                "moveto", remote_path(move["source"]), remote_path(move["destination"]),
            ], maximum=250_000, timeout=900)
            if stat_any(move["destination"], missing_ok=True) is None:
                raise ValueError(f"Move destination could not be verified: {move['destination']}")
            if stat_any(move["source"], missing_ok=True) is not None:
                raise ValueError(f"Move source still exists after move: {move['source']}")
            completed.append(current)
            append_audit("operation_completed", {"plan_token": token, "operation": current})
    except (ValueError, OSError, RcloneError, subprocess.SubprocessError) as error:
        append_audit("plan_stopped", {
            "plan_token": token,
            "completed": completed,
            "failed_operation": current,
            "error": str(error),
        }, required=False)
        patch_index(plan, stale=True, completed=completed)
        return {
            "status": "stopped_after_failure",
            "completed": completed,
            "failed_operation": current,
            "error": str(error),
            "rolled_back": False,
            "index_stale": True,
            "next_step": "Inspect Drime, then run refresh_index and create a new plan.",
        }
    index_refreshed = False
    index_stale = False
    try:
        index_refreshed = refresh_index_after_plan()
        if not index_refreshed:
            patch_index(plan, stale=False)
    except Exception:
        append_audit("index_update_failed", {"plan_token": token, "completed": completed}, required=False)
        index_refreshed = False
        index_stale = True
    append_audit("plan_completed", {
        "plan_token": token,
        "completed": completed,
        "index_refreshed": index_refreshed,
    }, required=False)
    return {
        "status": "completed",
        "completed": completed,
        "rolled_back": False,
        "index_refreshed": index_refreshed,
        "index_stale": index_stale,
    }


TOOLS = [
    {
        "name": "list_folder",
        "description": "List one live Drime folder. Read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative folder path; empty means root"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ENTRIES},
        }, "additionalProperties": False},
    },
    {
        "name": "get_info",
        "description": "Get live metadata for one Drime file or folder. Read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "refresh_index",
        "description": "Explicitly rebuild the local filename/metadata index from Drime. Read-only in Drime.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_index",
        "description": "Search the cached Drime filename/path index. Does not search file contents.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["any", "file", "folder"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }, "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "read_text",
        "description": "Read one approved UTF-8 text/subtitle/NFO/playlist file, up to 2,000,000 bytes.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False},
    },
    {
        "name": "plan_organization",
        "description": "Preflight up to 100 folder creations and moves, then return an exact preview and single-use approval token. Makes no Drime changes.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": {
            "create_folders": {"type": "array", "items": {"type": "string"}},
            "moves": {"type": "array", "items": {"type": "object", "properties": {
                "source": {"type": "string"}, "destination": {"type": "string"},
            }, "required": ["source", "destination"], "additionalProperties": False}},
        }, "additionalProperties": False},
    },
    {
        "name": "execute_organization_plan",
        "description": "Execute one exact, unexpired plan after user approval. Refuses to overwrite or merge at plan time; a concurrent writer can still race the final move (see README); stops on first failure; does not roll back.",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        "inputSchema": {"type": "object", "properties": {"plan_token": {"type": "string"}},
                        "required": ["plan_token"], "additionalProperties": False},
    },
]

HANDLERS = {
    "list_folder": list_folder,
    "get_info": get_info,
    "refresh_index": refresh_index,
    "search_index": search_index,
    "read_text": read_text,
    "plan_organization": plan_organization,
    "execute_organization_plan": execute_organization_plan,
}


def handle(message: dict) -> dict | None:
    if "id" not in message:
        return None
    identifier = message["id"]
    method = message.get("method")
    try:
        if method == "initialize":
            wanted = message.get("params", {}).get("protocolVersion")
            protocol = wanted if wanted in PROTOCOLS else "2025-11-25"
            result = {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "drime-organizer", "version": VERSION},
                "instructions": (
                    "Drime media organizer. Read tools may run automatically. Always show the full "
                    "plan_organization preview and obtain explicit user approval before calling "
                    "execute_organization_plan. No upload, delete, purge, share, or sync tools exist."
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            if name not in HANDLERS:
                return {"jsonrpc": "2.0", "id": identifier,
                        "error": {"code": -32602, "message": "Unknown tool"}}
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            try:
                value = HANDLERS[name](arguments)
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": identifier,
                    "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}
    except Exception:
        return {"jsonrpc": "2.0", "id": identifier,
                "error": {"code": -32603, "message": "Internal error"}}


def main() -> None:
    remote_root()
    rclone_config()
    for line in sys.stdin:
        try:
            if len(line) > MAX_REQUEST_BYTES:
                continue
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response = handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            continue


if __name__ == "__main__":
    main()
