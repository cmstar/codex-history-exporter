import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import export_codex_history as exporter

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10 do not include tomllib.
    tomllib = None


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def meta_row(thread_id="thread-1", cwd=r"C:\Work\Alpha", source="vscode"):
    return {
        "timestamp": "2026-07-19T01:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "timestamp": "2026-07-19T01:00:00Z",
            "cwd": cwd,
            "source": source,
            "thread_source": "user",
        },
    }


def user_row(text="用户问题", timestamp="2026-07-19T01:01:00Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def agent_event_row(
    text="最终回答", phase="final_answer", timestamp="2026-07-19T01:02:00Z"
):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text, "phase": phase},
    }


def agent_response_row(text="回退回答", timestamp="2026-07-19T01:02:00Z"):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def conversation_stub(thread_id, cwd):
    return exporter.ParsedConversation(
        thread_id=thread_id,
        cwd=cwd,
        source_path=Path("rollout.jsonl"),
        messages=(),
        last_timestamp=datetime(2026, 7, 19, tzinfo=timezone.utc),
        archived=False,
    )


class RolloutParsingTests(unittest.TestCase):
    def test_extracts_only_user_messages_and_final_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    meta_row(),
                    {
                        "timestamp": "2026-07-19T01:00:30Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "内部提示"}],
                        },
                    },
                    user_row(),
                    agent_event_row("过程说明", phase="commentary"),
                    agent_response_row("最终回答"),
                    agent_event_row("最终回答"),
                    {
                        "timestamp": "2026-07-19T01:03:00Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_reasoning", "text": "隐藏推理"},
                    },
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertIsNotNone(conversation)
            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "用户问题"), ("assistant", "最终回答")],
            )
            markdown = exporter.render_markdown(conversation, "会话标题", "Alpha")
            self.assertIn("# 会话标题", markdown)
            self.assertIn("## User\n\n用户问题", markdown)
            self.assertIn("## Codex\n\n最终回答", markdown)
            self.assertNotIn("内部提示", markdown)
            self.assertNotIn("过程说明", markdown)
            self.assertNotIn("隐藏推理", markdown)

    def test_excludes_subagent_rollouts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            row = meta_row()
            row["payload"]["source"] = {"subagent": {"other": "guardian"}}
            row["payload"]["thread_source"] = "subagent"
            write_jsonl(path, [row, user_row(), agent_event_row()])

            self.assertIsNone(exporter.parse_rollout(path))

    def test_falls_back_to_response_item_when_final_event_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(path, [meta_row(), user_row(), agent_response_row("旧版回答")])

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "用户问题"), ("assistant", "旧版回答")],
            )


class MetadataAndProjectTests(unittest.TestCase):
    def test_title_precedence_uses_session_index_then_state_database(self):
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp)
            (codex_home / "session_index.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "thread-index",
                                "thread_name": "旧名称",
                                "updated_at": "2026-07-18T00:00:00Z",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "id": "thread-index",
                                "thread_name": "索引名称",
                                "updated_at": "2026-07-19T00:00:00Z",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            database = sqlite3.connect(codex_home / "state_5.sqlite")
            database.execute("CREATE TABLE threads (id TEXT, title TEXT, updated_at INTEGER)")
            database.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                [
                    ("thread-index", "数据库旧名称", 1),
                    ("thread-db", "数据库名称", 2),
                ],
            )
            database.commit()
            database.close()

            titles = exporter.load_titles(codex_home)

            self.assertEqual(titles["thread-index"], "索引名称")
            self.assertEqual(titles["thread-db"], "数据库名称")

    def test_project_resolution_uses_explicit_then_projectless_then_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp)
            state = {
                "local-projects": {
                    "p1": {
                        "id": "p1",
                        "name": "Alpha Project",
                        "rootPaths": [r"C:\Work\Alpha"],
                        "createdAt": 1,
                        "updatedAt": 1,
                    }
                },
                "thread-project-assignments": {
                    "thread-explicit": {
                        "projectKind": "local",
                        "projectId": "p1",
                        "cwd": r"C:\Elsewhere",
                        "pendingCoreUpdate": False,
                    }
                },
                "projectless-thread-ids": ["thread-chat"],
            }
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            resolver = exporter.ProjectResolver.from_codex_home(codex_home)

            self.assertEqual(
                resolver.resolve(conversation_stub("thread-explicit", r"C:\Elsewhere")),
                "Alpha Project",
            )
            self.assertEqual(
                resolver.resolve(conversation_stub("thread-chat", r"C:\Work\Alpha")),
                "chat",
            )
            self.assertEqual(
                resolver.resolve(
                    conversation_stub("thread-cwd", r"C:\Work\Alpha\src")
                ),
                "Alpha Project",
            )
            self.assertEqual(
                resolver.resolve(conversation_stub("thread-other", r"C:\Work\Other")),
                "Other",
            )
            self.assertEqual(
                resolver.resolve(conversation_stub("thread-home", str(Path.home()))),
                "chat",
            )

    def test_project_resolution_also_reports_the_indexed_working_directory(self):
        projects = {
            "p1": {
                "name": "Alpha Project",
                "rootPaths": [r"C:\Work\Alpha", r"D:\Mirrors\Alpha"],
            }
        }
        assignments = {
            "thread-explicit": {"projectKind": "local", "projectId": "p1"}
        }
        resolver = exporter.ProjectResolver(
            projects, assignments, ["thread-chat"]
        )

        explicit = resolver.resolve_with_path(
            conversation_stub("thread-explicit", r"D:\Mirrors\Alpha\src")
        )
        projectless = resolver.resolve_with_path(
            conversation_stub("thread-chat", r"C:\Work\Alpha")
        )
        inferred = resolver.resolve_with_path(
            conversation_stub("thread-other", r"C:\Work\Other")
        )

        self.assertEqual(explicit.name, "Alpha Project")
        self.assertEqual(explicit.path, r"D:\Mirrors\Alpha")
        self.assertEqual(projectless.name, "chat")
        self.assertIsNone(projectless.path)
        self.assertEqual(inferred.name, "Other")
        self.assertEqual(inferred.path, r"C:\Work\Other")

    @unittest.skipIf(tomllib is None, "tomllib is unavailable before Python 3.11")
    def test_project_index_is_valid_toml_and_supports_multiple_paths(self):
        rendered = exporter.render_project_index(
            {
                "Alpha Project": {r"C:\Work\Alpha"},
                "多目录": {r"C:\One", r"D:\Two"},
            }
        )

        parsed = tomllib.loads(rendered)

        self.assertEqual(parsed["Alpha Project"]["Path"], r"C:\Work\Alpha")
        self.assertEqual(
            set(parsed["多目录"]["Paths"]), {r"C:\One", r"D:\Two"}
        )
        self.assertNotIn("chat", parsed)

    def test_safe_component_handles_invalid_and_reserved_windows_names(self):
        self.assertEqual(exporter.safe_component('a<b>:c"d/e\\f|g?h*', "fallback"), "a_b__c_d_e_f_g_h_")
        self.assertEqual(exporter.safe_component("CON", "fallback"), "_CON")
        self.assertEqual(exporter.safe_component("...", "fallback"), "fallback")


class EndToEndTests(unittest.TestCase):
    def make_codex_home(self, root):
        codex_home = root / "codex-home"
        sessions = codex_home / "sessions" / "2026" / "07" / "19"
        write_jsonl(
            sessions / "one.jsonl",
            [meta_row("thread-one"), user_row("问题一"), agent_event_row("回答一")],
        )
        write_jsonl(
            sessions / "two.jsonl",
            [meta_row("thread-two"), user_row("问题二"), agent_event_row("回答二")],
        )
        subagent = meta_row("thread-sub")
        subagent["payload"]["source"] = {"subagent": {"other": "guardian"}}
        write_jsonl(
            sessions / "sub.jsonl",
            [subagent, user_row("内部任务"), agent_event_row("内部结果")],
        )
        (codex_home / "session_index.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "id": thread_id,
                        "thread_name": "相同标题",
                        "updated_at": "2026-07-19T01:02:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for thread_id in ("thread-one", "thread-two")
            ),
            encoding="utf-8",
        )
        return codex_home

    def test_export_rebuilds_output_and_resolves_filename_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / ".output"
            output.mkdir()
            (output / "stale.md").write_text("old", encoding="utf-8")

            summary = exporter.export_history(codex_home, output)

            files = sorted(output.rglob("*.md"))
            self.assertEqual(summary.exported, 2)
            self.assertEqual(summary.excluded_subagents, 1)
            self.assertEqual(len(files), 2)
            self.assertFalse((output / "stale.md").exists())
            self.assertNotEqual(files[0].name, files[1].name)
            self.assertTrue(all(path.parent.name == "Alpha" for path in files))
            self.assertTrue((output / "projects.toml").is_file())
            if tomllib is not None:
                index = tomllib.loads(
                    (output / "projects.toml").read_text(encoding="utf-8")
                )
                self.assertEqual(index["Alpha"]["Path"], r"C:\Work\Alpha")

    def test_cli_defaults_to_current_working_directory_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            working_directory = root / "working"
            working_directory.mkdir()

            with mock.patch.object(Path, "cwd", return_value=working_directory):
                exit_code = exporter.main(["--codex-home", str(codex_home)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                len(list((working_directory / "output").rglob("*.md"))), 2
            )

    def test_cli_declines_to_replace_a_nonempty_output_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "existing-output"
            output.mkdir()
            stale = output / "keep.txt"
            stale.write_text("keep", encoding="utf-8")

            with mock.patch("builtins.input", return_value="n"):
                exit_code = exporter.main(
                    ["--codex-home", str(codex_home), "--output", str(output)]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stale.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(output.rglob("*.md")), [])

    def test_cli_replaces_a_nonempty_output_after_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "confirmed-output"
            output.mkdir()
            stale = output / "stale.txt"
            stale.write_text("old", encoding="utf-8")

            with mock.patch("builtins.input", return_value="yes"):
                exit_code = exporter.main(
                    ["--codex-home", str(codex_home), "--output", str(output)]
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(stale.exists())
            self.assertEqual(len(list(output.rglob("*.md"))), 2)

    def test_cli_force_replaces_custom_output_without_prompting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "custom-output"
            output.mkdir()
            (output / "stale.txt").write_text("old", encoding="utf-8")

            with mock.patch(
                "builtins.input", side_effect=AssertionError("must not prompt")
            ):
                exit_code = exporter.main(
                    ["--codex-home", str(codex_home), "-o", str(output), "-f"]
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse((output / "stale.txt").exists())
            self.assertEqual(len(list(output.rglob("*.md"))), 2)


class OutputSafetyTests(unittest.TestCase):
    def test_absolute_output_path_normalization_does_not_resolve_links(self):
        relative = Path(".output")

        with mock.patch.object(
            Path, "resolve", side_effect=AssertionError("resolve follows links")
        ):
            normalized = exporter._absolute_without_resolving(relative)

        self.assertTrue(normalized.is_absolute())
        self.assertEqual(normalized.name, ".output")

    def test_publish_falls_back_when_windows_locks_output_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / ".output"
            staging = root / ".output.__staging__"
            backup = root / ".output.__backup__"
            output.mkdir()
            staging.mkdir()
            (output / "old.md").write_text("old", encoding="utf-8")
            (staging / "new.md").write_text("new", encoding="utf-8")
            original_rename = Path.rename

            def locked_output_rename(path, target):
                if path == output and Path(target) == backup:
                    raise PermissionError("output directory is in use")
                return original_rename(path, target)

            with mock.patch.object(Path, "rename", autospec=True, side_effect=locked_output_rename):
                exporter._publish_staging(staging, output, backup)

            self.assertFalse((output / "old.md").exists())
            self.assertEqual((output / "new.md").read_text(encoding="utf-8"), "new")
            self.assertFalse(staging.exists())
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
