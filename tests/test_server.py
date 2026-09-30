import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("drime_mcp_server", SERVER)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

FAKE_RCLONE = r'''#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_RCLONE_STATE"]

def load():
    with open(state_path, encoding="utf-8") as handle:
        return json.load(handle)

def save(state):
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

def clean(remote):
    prefix = "drime-mcp:"
    if not remote.startswith(prefix):
        sys.exit(9)
    return remote[len(prefix):].lstrip("/")

def metadata(path, item, shown=None):
    is_dir = item["is_dir"]
    size = -1 if is_dir else len(item.get("content", "").encode("utf-8"))
    return {"Path": shown if shown is not None else path, "Name": path.rsplit("/", 1)[-1], "IsDir": is_dir, "Size": size}

args = sys.argv[1:]
if len(args) < 2 or args[0] != "--config" or not args[1]:
    print("fake rclone requires a --config pair", file=sys.stderr)
    sys.exit(11)
args = args[2:]
state = load()
entries = state["entries"]
command = args[0]

if command == "lsjson":
    path = clean(args[1])
    if "--stat" in args:
        if path not in entries:
            print("not found", file=sys.stderr)
            sys.exit(3)
        print(json.dumps(metadata(path, entries[path])))
    elif "--recursive" in args:
        if os.environ.get("FAKE_RCLONE_FAIL_RECURSIVE"):
            print("simulated listing failure", file=sys.stderr)
            sys.exit(10)
        result = []
        for candidate, item in sorted(entries.items()):
            if path and not candidate.startswith(path + "/"):
                continue
            shown = candidate[len(path) + 1:] if path else candidate
            if shown:
                result.append(metadata(candidate, item, shown))
        print(json.dumps(result))
    else:
        if path and (path not in entries or not entries[path]["is_dir"]):
            print("directory not found", file=sys.stderr)
            sys.exit(3)
        prefix = path + "/" if path else ""
        result = []
        for candidate, item in sorted(entries.items()):
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix):]
            if remainder and "/" not in remainder:
                result.append(metadata(candidate, item, remainder))
        print(json.dumps(result))
elif command == "cat":
    path = clean(args[1])
    if path not in entries or entries[path]["is_dir"]:
        sys.exit(3)
    sys.stdout.write(entries[path].get("content", ""))
elif command == "mkdir":
    path = clean(args[1])
    if path in entries:
        sys.exit(1)
    parts = path.split("/")
    for index in range(1, len(parts) + 1):
        candidate = "/".join(parts[:index])
        entries.setdefault(candidate, {"is_dir": True})
    save(state)
elif command == "moveto":
    fail_after = os.environ.get("FAKE_RCLONE_AUTH_FAIL_AFTER_MOVES")
    if fail_after is not None:
        state.setdefault("moves_done", 0)
        if state["moves_done"] >= int(fail_after):
            print("authorization failed (HTTP 401): Drime token expired", file=sys.stderr)
            sys.exit(5)
        state["moves_done"] += 1
        save(state)
    source = clean(args[1])
    destination = clean(args[2])
    if destination == os.environ.get("FAKE_RCLONE_FAIL_DEST"):
        print("simulated network failure", file=sys.stderr)
        sys.exit(10)
    if source not in entries or destination in entries:
        sys.exit(3)
    parent = destination.rsplit("/", 1)[0] if "/" in destination else ""
    if parent and (parent not in entries or not entries[parent]["is_dir"]):
        sys.exit(3)
    moving = {path: item for path, item in entries.items() if path == source or path.startswith(source + "/")}
    for path in moving:
        del entries[path]
    for path, item in moving.items():
        entries[destination + path[len(source):]] = item
    save(state)
else:
    print("unsupported fake command", file=sys.stderr)
    sys.exit(8)
'''


class DrimeOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.fake = root / "fake-rclone"
        self.fake.write_text(FAKE_RCLONE, encoding="utf-8")
        self.fake.chmod(0o700)
        self.state = root / "state.json"
        self.state.write_text(json.dumps({"entries": {
            "Shows": {"is_dir": True},
            "Unsorted": {"is_dir": True},
            "Unsorted/episode:S01E01.mkv": {"is_dir": False, "content": "video"},
            "Unsorted/episode:S01E02.mkv": {"is_dir": False, "content": "video2"},
            "Notes": {"is_dir": True},
            "Notes/library.nfo": {"is_dir": False, "content": "hello media"},
            "Existing": {"is_dir": True},
        }}), encoding="utf-8")
        self.old_env = os.environ.copy()
        os.environ.update({
            "DRIME_MCP_REMOTE": "drime-mcp:",
            "RCLONE_BIN": str(self.fake),
            "RCLONE_CONFIG": str(root / "only-drime.conf"),
            "DRIME_MCP_INDEX": str(root / "index.json"),
            "DRIME_MCP_AUDIT": str(root / "audit.jsonl"),
            "FAKE_RCLONE_STATE": str(self.state),
        })
        server.PLANS.clear()
        self.addCleanup(self.restore_environment)

    def restore_environment(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        server.PLANS.clear()

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_path_guards_allow_media_colons_but_block_escape(self):
        self.assertEqual(
            server.remote_path("Unsorted/episode:S01E01.mkv"),
            "drime-mcp:Unsorted/episode:S01E01.mkv",
        )
        for bad in ("../escape", "Notes/../escape", "/absolute", "x//y", "x\\y", "x\ny"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                server.remote_path(bad)

    def test_invisible_and_bidi_characters_are_rejected(self):
        for bad in (
            "evi\u202el.txt", "x\u200by", "a\u00adb", "bom\ufeff",
            "line\u2028sep", "sur\ud800rogate",
        ):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                server.remote_path(bad)
        self.assertEqual(server.remote_path("persian\u200cname"), "drime-mcp:persian\u200cname")

    def test_c1_and_format_characters_rejected_zwj_allowed(self):
        for bad in ("x\u0085y", "x\u009by", "x\u0600y", "x\u061cy", "x\u2060y"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                server.remote_path(bad)
        self.assertEqual(server.remote_path("persian\u200dname"), "drime-mcp:persian\u200dname")

    def test_refresh_and_list_skip_non_conforming_names(self):
        state = self.read_state()
        state["entries"]["bad\u2060name.mkv"] = {"is_dir": False, "content": "x"}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        refreshed = server.refresh_index({})
        self.assertEqual(refreshed["skipped_entries"], 1)
        self.assertEqual(refreshed["indexed_entries"], 7)
        listed = server.list_folder({"path": ""})
        self.assertEqual(listed["skipped_entries"], 1)
        self.assertEqual(len(listed["entries"]), 4)
        state = self.read_state()
        del state["entries"]["bad\u2060name.mkv"]
        self.state.write_text(json.dumps(state), encoding="utf-8")
        clean = server.refresh_index({})
        self.assertEqual(clean["skipped_entries"], 0)

    def test_rclone_config_is_required_and_passed_to_every_invocation(self):
        self.assertEqual(server.rclone_config(), os.environ["RCLONE_CONFIG"])
        del os.environ["RCLONE_CONFIG"]
        with self.assertRaisesRegex(ValueError, "RCLONE_CONFIG must point"):
            server.rclone_config()
        with self.assertRaisesRegex(ValueError, "RCLONE_CONFIG must point"):
            server.run_rclone(["lsjson", "drime-mcp:"])
        os.environ["RCLONE_CONFIG"] = ""
        with self.assertRaisesRegex(ValueError, "RCLONE_CONFIG must point"):
            server.rclone_config()
        os.environ["RCLONE_CONFIG"] = str(Path(self.temp.name) / "only-drime.conf")
        # The fake rclone exits 11 unless every invocation starts with --config.
        self.assertTrue(server.run_rclone(["lsjson", "drime-mcp:"]))

    def test_main_fails_fast_without_rclone_config(self):
        env = {key: value for key, value in os.environ.items() if key != "RCLONE_CONFIG"}
        result = subprocess.run(
            [sys.executable, str(SERVER)],
            input="", text=True, capture_output=True, env=env, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RCLONE_CONFIG", result.stderr)

    def test_poisoned_index_never_masks_completed_operations(self):
        server.refresh_index({})
        index_path = Path(os.environ["DRIME_MCP_INDEX"])
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entries"][0] = {"is_dir": False}
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another remote or is invalid"):
            server.load_index()
        preview = server.plan_organization({"moves": [{
            "source": "Unsorted/episode:S01E01.mkv",
            "destination": "Shows/episode:S01E01.mkv",
        }]})
        os.environ["FAKE_RCLONE_FAIL_RECURSIVE"] = "1"
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], [{
            "action": "move",
            "source": "Unsorted/episode:S01E01.mkv",
            "destination": "Shows/episode:S01E01.mkv",
        }])
        self.assertFalse(result["index_refreshed"])
        entries = self.read_state()["entries"]
        self.assertIn("Shows/episode:S01E01.mkv", entries)
        self.assertNotIn("Unsorted/episode:S01E01.mkv", entries)

    def test_refresh_and_search_index(self):
        refreshed = server.refresh_index({})
        self.assertEqual(refreshed["indexed_entries"], 7)
        result = server.search_index({"query": "episode S01", "kind": "file"})
        self.assertEqual(result["matches"][0]["path"], "Unsorted/episode:S01E01.mkv")
        self.assertFalse(result["stale"])

    def test_read_text_with_two_megabyte_policy(self):
        result = server.read_text({"path": "Notes/library.nfo"})
        self.assertEqual(result["content"], "hello media")
        with self.assertRaises(ValueError):
            server.read_text({"path": "Unsorted/episode:S01E01.mkv"})

    def test_conflict_stops_during_preview(self):
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            server.plan_organization({"moves": [{
                "source": "Unsorted/episode:S01E01.mkv",
                "destination": "Existing",
            }]})

    def test_preview_then_execute_create_and_move(self):
        server.refresh_index({})
        preview = server.plan_organization({
            "create_folders": ["Shows/Test Show"],
            "moves": [{
                "source": "Unsorted/episode:S01E01.mkv",
                "destination": "Shows/Test Show/episode:S01E01.mkv",
            }],
        })
        before = self.read_state()["entries"]
        self.assertNotIn("Shows/Test Show", before)
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "completed")
        after = self.read_state()["entries"]
        self.assertNotIn("Unsorted/episode:S01E01.mkv", after)
        self.assertIn("Shows/Test Show/episode:S01E01.mkv", after)
        audit = Path(os.environ["DRIME_MCP_AUDIT"]).read_text(encoding="utf-8")
        self.assertIn('"event":"operation_completed"', audit)
        indexed = json.loads(Path(os.environ["DRIME_MCP_INDEX"]).read_text(encoding="utf-8"))
        indexed_paths = {entry["path"] for entry in indexed["entries"]}
        self.assertIn("Shows/Test Show/episode:S01E01.mkv", indexed_paths)

    def test_token_is_single_use(self):
        preview = server.plan_organization({"create_folders": ["Shows/New"]})
        server.execute_organization_plan({"plan_token": preview["plan_token"]})
        with self.assertRaisesRegex(ValueError, "already used"):
            server.execute_organization_plan({"plan_token": preview["plan_token"]})

    def test_failed_execute_preflight_is_audited(self):
        preview = server.plan_organization({"create_folders": ["Shows/New"]})
        state = self.read_state()
        state["entries"]["Shows/New"] = {"is_dir": True}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            server.execute_organization_plan({"plan_token": preview["plan_token"]})
        audit = Path(os.environ["DRIME_MCP_AUDIT"]).read_text(encoding="utf-8")
        self.assertIn('"event":"plan_rejected"', audit)

    def test_mid_batch_failure_stops_without_rollback(self):
        server.refresh_index({})
        preview = server.plan_organization({"moves": [
            {
                "source": "Unsorted/episode:S01E01.mkv",
                "destination": "Shows/episode:S01E01.mkv",
            },
            {
                "source": "Unsorted/episode:S01E02.mkv",
                "destination": "Shows/episode:S01E02.mkv",
            },
        ]})
        os.environ["FAKE_RCLONE_FAIL_DEST"] = "Shows/episode:S01E02.mkv"
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "stopped_after_failure")
        self.assertEqual(len(result["completed"]), 1)
        self.assertFalse(result["rolled_back"])
        entries = self.read_state()["entries"]
        self.assertIn("Shows/episode:S01E01.mkv", entries)
        self.assertIn("Unsorted/episode:S01E02.mkv", entries)
        index = json.loads(Path(os.environ["DRIME_MCP_INDEX"]).read_text(encoding="utf-8"))
        indexed_paths = {entry["path"] for entry in index["entries"]}
        self.assertTrue(index["stale"])
        self.assertIn("Shows/episode:S01E01.mkv", indexed_paths)
        self.assertIn("Unsorted/episode:S01E02.mkv", indexed_paths)

    def test_execute_stops_on_expired_auth(self):
        server.refresh_index({})
        preview = server.plan_organization({"moves": [
            {
                "source": "Unsorted/episode:S01E01.mkv",
                "destination": "Shows/episode:S01E01.mkv",
            },
            {
                "source": "Unsorted/episode:S01E02.mkv",
                "destination": "Shows/episode:S01E02.mkv",
            },
        ]})
        # The Drime token expires after the first move: the second moveto
        # fails with an authorization error while earlier stats still worked.
        os.environ["FAKE_RCLONE_AUTH_FAIL_AFTER_MOVES"] = "1"
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "stopped_after_failure")
        self.assertEqual(result["completed"], [{
            "action": "move",
            "source": "Unsorted/episode:S01E01.mkv",
            "destination": "Shows/episode:S01E01.mkv",
        }])
        self.assertEqual(result["failed_operation"], {
            "action": "move",
            "source": "Unsorted/episode:S01E02.mkv",
            "destination": "Shows/episode:S01E02.mkv",
        })
        self.assertIn("authorization failed", result["error"])
        self.assertFalse(result["rolled_back"])
        self.assertTrue(result["index_stale"])
        entries = self.read_state()["entries"]
        self.assertIn("Shows/episode:S01E01.mkv", entries)
        self.assertIn("Unsorted/episode:S01E02.mkv", entries)
        index = json.loads(Path(os.environ["DRIME_MCP_INDEX"]).read_text(encoding="utf-8"))
        indexed_paths = {entry["path"] for entry in index["entries"]}
        self.assertTrue(index["stale"])
        self.assertIn("Shows/episode:S01E01.mkv", indexed_paths)
        self.assertIn("Unsorted/episode:S01E02.mkv", indexed_paths)
        audit = Path(os.environ["DRIME_MCP_AUDIT"]).read_text(encoding="utf-8")
        self.assertIn('"event":"plan_stopped"', audit)
        self.assertIn("authorization failed", audit)

    def test_execute_refreshes_index_automatically(self):
        server.refresh_index({})
        state = self.read_state()
        state["entries"]["Unsorted/external-download.mkv"] = {"is_dir": False, "content": "new"}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        preview = server.plan_organization({"moves": [{
            "source": "Unsorted/episode:S01E01.mkv",
            "destination": "Shows/episode:S01E01.mkv",
        }]})
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["index_refreshed"])
        self.assertFalse(result["index_stale"])
        index = json.loads(Path(os.environ["DRIME_MCP_INDEX"]).read_text(encoding="utf-8"))
        indexed_paths = {entry["path"] for entry in index["entries"]}
        self.assertIn("Shows/episode:S01E01.mkv", indexed_paths)
        self.assertIn("Unsorted/external-download.mkv", indexed_paths)
        self.assertFalse(index["stale"])
        self.assertIsNone(index["patched_at"])

    def test_execute_falls_back_to_patch_when_refresh_fails(self):
        server.refresh_index({})
        preview = server.plan_organization({"moves": [{
            "source": "Unsorted/episode:S01E01.mkv",
            "destination": "Shows/episode:S01E01.mkv",
        }]})
        os.environ["FAKE_RCLONE_FAIL_RECURSIVE"] = "1"
        result = server.execute_organization_plan({"plan_token": preview["plan_token"]})
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["index_refreshed"])
        self.assertFalse(result["index_stale"])
        index = json.loads(Path(os.environ["DRIME_MCP_INDEX"]).read_text(encoding="utf-8"))
        indexed_paths = {entry["path"] for entry in index["entries"]}
        self.assertIn("Shows/episode:S01E01.mkv", indexed_paths)
        self.assertNotIn("Unsorted/episode:S01E01.mkv", indexed_paths)
        self.assertFalse(index["stale"])
        self.assertIsNotNone(index["patched_at"])

    def test_stdio_exposes_only_expected_tools(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "delete", "arguments": {}}},
        ]
        result = subprocess.run(
            [sys.executable, str(SERVER)],
            input="\n".join(map(json.dumps, requests)) + "\n",
            text=True,
            capture_output=True,
            env=os.environ,
            timeout=15,
            check=True,
        )
        replies = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
        names = [tool["name"] for tool in replies[2]["result"]["tools"]]
        self.assertEqual(names, [
            "list_folder", "get_info", "refresh_index", "search_index", "read_text",
            "plan_organization", "execute_organization_plan",
        ])
        self.assertEqual(replies[3]["error"]["code"], -32602)

    def test_stdio_survives_over_deep_json_frame(self):
        depth = 2000
        nested = "[" * depth + "1" + "]" * depth
        hostile = ('{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                   '"params":{"name":"noop","arguments":{"payload":' + nested + "}}}")
        ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        result = subprocess.run(
            [sys.executable, str(SERVER)],
            input=hostile + "\n" + ping + "\n",
            text=True,
            capture_output=True,
            env=os.environ,
            timeout=15,
            check=True,
        )
        replies = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
        self.assertNotIn(1, replies)
        self.assertEqual(replies[2]["result"], {})

    def test_stdio_skips_oversized_line(self):
        oversized = ('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"padding":"'
                     + "x" * (server.MAX_REQUEST_BYTES + 10) + '"}}')
        ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        result = subprocess.run(
            [sys.executable, str(SERVER)],
            input=oversized + "\n" + ping + "\n",
            text=True,
            capture_output=True,
            env=os.environ,
            timeout=15,
            check=True,
        )
        replies = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
        self.assertNotIn(1, replies)
        self.assertEqual(replies[2]["result"], {})


if __name__ == "__main__":
    unittest.main()
