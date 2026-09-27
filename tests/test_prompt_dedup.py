"""G2/R3 acceptance tests: in-window read_file observation deduplication in prompts.

Maps 1:1 to docs/tech-design.md §2.9 (R3 acceptances 1-3; R3-4 lives in test_benchmark.py).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.agent import AgentLoop
from repair_agent.domain import Finding, Observation, RepairTask, Severity, ToolStatus
from repair_agent.models import ScriptedModel
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.executor import ToolExecutor


@contextmanager
def temp_dir(testcase: unittest.TestCase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    yield Path(temporary.name)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "HEAL test")
    git(repo, "config", "user.email", "heal-test@localhost")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src" / "sample.c").write_bytes(b"int value = OLD;\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def dedup_loop(repo: Path, base: str, *, enabled: bool) -> AgentLoop:
    task = RepairTask(task_id="task-1", run_id="run-1", repo=str(repo), base_commit=base, issues=(Finding("finding-1", "R001", Severity.LOW, "src/sample.c", 1, "m"),))
    return AgentLoop(task, worker_id="worker", model=ScriptedModel([]), executor=ToolExecutor(WorkspaceState(repo, base)), dedup_recent_observations=enabled)


def read_observation(call_id: str, *, path: str = "src/a.cpp", start: int = 1, end: int = 30, content_hash: str = "hash-1", status: ToolStatus = ToolStatus.OK, complete: bool = True, text: str = "line\n" * 40) -> Observation:
    return Observation(
        tool_call_id=call_id, tool="read_file", status=status,
        content={"path": path, "start_line": start, "end_line": end, "text": text, "content_hash": content_hash},
        source_paths=(path,), complete=complete,
    )


def refs(payloads: list[dict]) -> list[dict]:
    return [item["observation_ref"] for item in payloads if "observation_ref" in item]


class PromptDedupTests(unittest.TestCase):
    def test_identical_reads_are_referenced_to_first_occurrence(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            observations = [read_observation("call-1"), read_observation("call-2")]
            payloads = loop._prompt_observations(observations)
            self.assertEqual(len(payloads), 2)
            self.assertNotIn("observation_ref", payloads[0])
            self.assertEqual(payloads[0]["content"]["text"], observations[0].content["text"])
            reference = refs(payloads)[0]
            self.assertEqual(reference["replays_tool_call_id"], "call-1")
            self.assertEqual((reference["path"], reference["start_line"], reference["end_line"], reference["content_hash"]), ("src/a.cpp", 1, 30, "hash-1"))
            self.assertEqual(payloads[1]["tool_call_id"], "call-2")
            self.assertNotIn("content", payloads[1])
            # three identical reads: one full copy, two single-hop references
            payloads = loop._prompt_observations([read_observation("call-1"), read_observation("call-2"), read_observation("call-3")])
            self.assertEqual(len(refs(payloads)), 2)
            self.assertTrue(all(item["replays_tool_call_id"] == "call-1" for item in refs(payloads)))
            self.assertEqual([item["tool_call_id"] for item in payloads], ["call-1", "call-2", "call-3"])

    def test_changed_content_hash_is_never_deduplicated(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            payloads = loop._prompt_observations([read_observation("call-1", content_hash="hash-1"), read_observation("call-2", content_hash="hash-2")])
            self.assertEqual(refs(payloads), [])
            self.assertEqual([item["content"]["content_hash"] for item in payloads], ["hash-1", "hash-2"])

    def test_different_range_is_never_deduplicated(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            payloads = loop._prompt_observations([read_observation("call-1", end=30), read_observation("call-2", end=31)])
            self.assertEqual(refs(payloads), [])

    def test_non_ok_or_incomplete_reads_are_never_deduplicated(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            truncated = read_observation("call-2", status=ToolStatus.TRUNCATED, complete=False, text="x" * 10)
            payloads = loop._prompt_observations([read_observation("call-1"), truncated])
            self.assertEqual(refs(payloads), [])
            partial = read_observation("call-2", status=ToolStatus.PARTIAL)
            payloads = loop._prompt_observations([read_observation("call-1"), partial])
            self.assertEqual(refs(payloads), [])

    def test_other_tools_and_ineligible_content_are_never_deduplicated(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            search = Observation(tool_call_id="s1", tool="search_code", status=ToolStatus.OK, content={"path": "src/a.cpp", "start_line": 1, "end_line": 30, "text": "x", "content_hash": "hash-1"}, complete=True)
            payloads = loop._prompt_observations([read_observation("call-1"), search])
            self.assertEqual(refs(payloads), [])
            no_hash = Observation(tool_call_id="c2", tool="read_file", status=ToolStatus.OK, content={"path": "src/a.cpp", "start_line": 1, "end_line": 30, "text": "x"}, source_paths=("src/a.cpp",), complete=True)
            payloads = loop._prompt_observations([read_observation("call-1"), no_hash])
            self.assertEqual(refs(payloads), [])

    def test_switch_default_off_keeps_full_primitives(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=False)
            payloads = loop._prompt_observations([read_observation("call-1"), read_observation("call-2")])
            self.assertEqual(refs(payloads), [])
            self.assertEqual(len(payloads), 2)
            self.assertIn("text", payloads[1]["content"])

    def test_reference_items_are_exempt_from_char_trim(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            task = RepairTask(task_id="task-1", run_id="run-1", repo=str(repo), base_commit=base, issues=(Finding("finding-1", "R001", Severity.LOW, "src/sample.c", 1, "m"),))
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel([]), executor=ToolExecutor(WorkspaceState(repo, base)), dedup_recent_observations=True, max_observation_chars=200)
            payloads = loop._prompt_observations([read_observation("call-1"), read_observation("call-2")])
            # the retained first occurrence is trimmed exactly as today (content becomes a bounded string)
            self.assertTrue(str(payloads[0]["content"]).endswith("…[truncated]"))
            self.assertFalse(payloads[0]["complete"])
            reference = refs(payloads)[0]
            self.assertEqual(reference["replays_tool_call_id"], "call-1")

    def test_out_of_window_reads_collapse_into_history_as_before(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            loop = dedup_loop(repo, base, enabled=True)
            loop.max_recent_observations = 2
            payloads = loop._prompt_observations([read_observation("call-1"), read_observation("call-2"), read_observation("call-3")])
            self.assertIn("historical_summary", payloads[0])
            # within the trailing window the identical read is still deduplicated
            self.assertEqual(len(refs(payloads)), 1)
            self.assertEqual(refs(payloads)[0]["replays_tool_call_id"], "call-2")


if __name__ == "__main__":
    unittest.main()
