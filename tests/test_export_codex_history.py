import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def completed_user_item_row(text="用户问题", timestamp="2026-07-19T01:01:00Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "UserMessage",
                "id": "user-item",
                "content": {"type": "text", "text": text},
            },
        },
    }


def completed_agent_item_row(
    text="最终回答", phase="final_answer", timestamp="2026-07-19T01:02:00Z"
):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "AgentMessage",
                "id": "agent-item",
                "content": {"type": "Text", "text": text},
                "phase": phase,
            },
        },
    }


def rollback_row(num_turns=1, timestamp="2026-07-19T01:03:00Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "thread_rolled_back", "num_turns": num_turns},
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


class InheritedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex"
        self.output = Path(self.temp.name) / "output"
        self.base = self.home / "sessions" / "base.jsonl"
        write_jsonl(self.base, [
            meta_row(), user_row("前文"), agent_event_row("前文回答"),
            user_row("失效问题", "2026-07-19T01:03:00Z"),
            agent_event_row("失效回答", timestamp="2026-07-19T01:04:00Z"),
        ])

    def branch(self, name="branch", parent=None, count=3, timestamp="2026-07-20T01:00:00Z", rows=None):
        parent = parent or self.base
        raw = parent.read_bytes().splitlines(keepends=True)
        meta = meta_row()
        meta["timestamp"] = meta["payload"]["timestamp"] = timestamp
        meta["payload"]["history_mode"] = "paginated"
        meta["payload"]["history_base"] = {
            "thread_id": "thread-1", "end_ordinal_exclusive": count,
            "end_byte_offset": sum(map(len, raw[:count])),
        }
        path = self.home / "sessions" / (name + ".jsonl")
        write_jsonl(path, [meta] + (rows if rows is not None else [
            user_row("修改后的问题", "2026-07-20T01:01:00Z"),
            agent_event_row("新回答", timestamp="2026-07-20T01:02:00Z"),
        ]))
        return path

    def active(self, path, archived=0):
        connection = sqlite3.connect(self.home / "state_9.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, ?, ?)", ("thread-1", str(path), archived))
            connection.commit()
        finally:
            connection.close()

    def export(self, **kwargs):
        summary = exporter.export_history(self.home, self.output, **kwargs)
        files = list(self.output.rglob("*.md"))
        self.assertEqual(summary.exported, 1)
        self.assertEqual(len(files), 1)
        return summary, files[0].read_text(encoding="utf-8")

    def test_selected_thread_restores_prefix_and_replaces_old_branch(self):
        current = self.branch()
        self.active(current)
        summary, text = self.export(session_id="codex://threads/thread-1")
        self.assertEqual(summary.merged_rollouts, 1)
        self.assertEqual(text.count("# 👤 User"), 2)
        self.assertEqual(text.count("# 🤖 Codex"), 2)
        self.assertIn("前文回答", text)
        self.assertIn("修改后的问题", text)
        self.assertNotIn("失效", text)
        self.assertFalse((self.output / "projects.toml").exists())

    def test_unique_history_leaf_without_database(self):
        self.branch()
        _, text = self.export()
        self.assertIn("前文回答", text)
        self.assertNotIn("失效", text)

    def test_multilevel_inheritance(self):
        first = self.branch()
        last = self.branch("last", first, count=3, timestamp="2026-07-21T01:00:00Z", rows=[
            user_row("第三轮", "2026-07-21T01:01:00Z"),
            agent_event_row("第三轮回答", timestamp="2026-07-21T01:02:00Z"),
        ])
        self.active(last)
        _, text = self.export()
        self.assertEqual(text.count("# 👤 User"), 3)
        self.assertLess(text.index("前文回答"), text.index("新回答"))
        self.assertLess(text.index("新回答"), text.index("第三轮回答"))
        self.assertNotIn("失效", text)

    def test_rollback_applies_to_inherited_turns(self):
        self.branch(count=5, rows=[
            rollback_row(1, "2026-07-20T01:00:00Z"),
            user_row("回滚后", "2026-07-20T01:01:00Z"),
            agent_event_row("回滚后回答", timestamp="2026-07-20T01:02:00Z"),
        ])
        _, text = self.export()
        self.assertIn("前文回答", text)
        self.assertIn("回滚后回答", text)
        self.assertNotIn("失效", text)

    def test_archived_base_copy_does_not_duplicate_messages(self):
        duplicate = self.home / "archived_sessions" / "copy.jsonl"
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.base.read_bytes())
        self.branch()
        _, text = self.export()
        self.assertEqual(text.count("# 👤 User"), 2)

    def test_creation_filter_uses_original_creation_and_last_chat_uses_new_branch(self):
        current = self.branch()
        self.active(current)
        _, text = self.export(created_since="20260719", created_until="20260719",
                              last_chat_since="20260720", last_chat_until="20260720")
        self.assertIn("新回答", text)

    def test_current_database_path_wins_over_newer_unused_branch(self):
        current = self.branch()
        self.branch("unused", timestamp="2026-07-21T01:00:00Z", rows=[
            user_row("不应导出", "2026-07-21T01:01:00Z"),
        ])
        self.active(current)
        _, text = self.export()
        self.assertIn("新回答", text)
        self.assertNotIn("不应导出", text)

    def test_relocated_database_path_uses_unique_filename(self):
        current = self.branch()
        self.active("X:\\old-computer\\sessions\\" + current.name)
        self.export()

    def test_archive_filter_applies_after_reconstruction(self):
        current = self.branch()
        self.active(current, archived=1)
        summary = exporter.export_history(self.home, self.output, ignore_archived=True)
        self.assertEqual(summary.exported, 0)
        self.assertEqual(summary.ignored_archived, 1)

    def test_missing_base_preserves_existing_output(self):
        self.branch()
        self.base.unlink()
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "history_base"):
            exporter.export_history(self.home, self.output)
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.output.with_name("output.__staging__").exists())

    def test_incorrect_byte_boundary_is_rejected(self):
        current = self.branch()
        rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["history_base"]["end_byte_offset"] += 1
        write_jsonl(current, rows)
        with self.assertRaisesRegex(RuntimeError, "history_base"):
            exporter.export_history(self.home, self.output)

    def test_crlf_and_non_ascii_byte_boundary(self):
        self.base.write_bytes(self.base.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.branch()
        _, text = self.export()
        self.assertIn("前文回答", text)

    def test_ambiguous_current_branch_is_rejected(self):
        self.branch()
        self.branch("other", rows=[user_row("其他分支")])
        with self.assertRaisesRegex(RuntimeError, "ambiguous current rollout"):
            exporter.export_history(self.home, self.output)

    def test_same_size_but_different_base_prefixes_are_rejected(self):
        other = self.home / "sessions" / "other-base.jsonl"
        other.write_bytes(self.base.read_bytes().replace("前文".encode(), "异文".encode()))
        current = self.branch()
        self.active(current)
        with self.assertRaisesRegex(RuntimeError, "ambiguous history_base"):
            exporter.export_history(self.home, self.output)

    def test_invalid_record_boundary_is_rejected(self):
        current = self.branch()
        rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["history_base"]["end_ordinal_exclusive"] = True
        write_jsonl(current, rows)
        with self.assertRaisesRegex(RuntimeError, "invalid history_base boundary"):
            exporter.export_history(self.home, self.output)

    def test_cycle_fails_without_recursing_forever(self):
        meta = meta_row()
        meta["payload"]["history_base"] = {
            "thread_id": "thread-1", "end_ordinal_exclusive": 1, "end_byte_offset": 0,
        }
        # Stabilize the self-referencing metadata's byte count.
        for _ in range(5):
            write_jsonl(self.base, [meta])
            meta["payload"]["history_base"]["end_byte_offset"] = self.base.stat().st_size
        write_jsonl(self.base, [meta])
        other = self.home / "sessions" / "cycle.jsonl"
        other.write_bytes(self.base.read_bytes())
        with self.assertRaisesRegex(RuntimeError, "history_base"):
            exporter.export_history(self.home, self.output)

    def test_zero_boundary_needs_no_base_file(self):
        current = self.branch(count=0)
        self.base.unlink()
        self.active(current)
        _, text = self.export()
        self.assertNotIn("前文", text)
        self.assertIn("新回答", text)

    def test_missing_current_database_path_is_rejected(self):
        self.branch()
        self.active(self.home / "sessions" / "missing.jsonl")
        with self.assertRaisesRegex(RuntimeError, "cannot locate current rollout"):
            exporter.export_history(self.home, self.output)

    def test_unrelated_broken_history_does_not_block_selected_thread(self):
        current = self.branch()
        rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["id"] = "unrelated"
        rows[0]["payload"]["history_base"]["thread_id"] = "missing"
        write_jsonl(current, rows)
        _, text = self.export(session_id="thread-1")
        self.assertIn("前文回答", text)


class SessionSelectorTests(unittest.TestCase):
    def test_keeps_a_plain_session_id(self):
        self.assertEqual(
            exporter._normalize_session_selector("  thread-one  "), "thread-one"
        )

    def test_extracts_session_id_from_codex_thread_deep_link(self):
        self.assertEqual(
            exporter._normalize_session_selector("codex://threads/thread-two"),
            "thread-two",
        )

    def test_rejects_a_malformed_codex_deep_link(self):
        with self.assertRaisesRegex(ValueError, "expected codex://threads"):
            exporter._normalize_session_selector("codex://thread/thread-two")


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
                    agent_response_row("最终回答\n\n## 回答小节"),
                    agent_event_row("最终回答\n\n## 回答小节"),
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
                [("user", "用户问题"), ("assistant", "最终回答\n\n## 回答小节")],
            )
            markdown = exporter.render_markdown(conversation, "会话标题", "Alpha")
            self.assertIn("# 会话标题", markdown)
            self.assertIn("\n# 👤 User\n\n用户问题", markdown)
            self.assertIn("\n# 🤖 Codex\n\n最终回答\n\n## 回答小节", markdown)
            self.assertNotIn("\n## 👤 User\n\n", markdown)
            self.assertNotIn("\n## 🤖 Codex\n\n", markdown)
            self.assertNotIn("内部提示", markdown)
            self.assertNotIn("过程说明", markdown)
            self.assertNotIn("隐藏推理", markdown)

    def test_extracts_current_item_completed_messages(self):
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
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "内部环境上下文"}
                            ],
                        },
                    },
                    completed_user_item_row("新格式问题"),
                    completed_agent_item_row("过程说明", phase="commentary"),
                    agent_response_row("新格式回答"),
                    completed_agent_item_row("新格式回答"),
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "新格式问题"), ("assistant", "新格式回答")],
            )

    def test_extracts_documented_app_server_item_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    meta_row(),
                    {
                        "timestamp": "2026-07-19T01:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "userMessage",
                                "id": "user-item",
                                "content": [
                                    {"type": "text", "text": "官方结构问题"}
                                ],
                            },
                        },
                    },
                    {
                        "timestamp": "2026-07-19T01:02:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "agentMessage",
                                "id": "agent-item",
                                "text": "官方结构回答",
                                "phase": "final_answer",
                            },
                        },
                    },
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "官方结构问题"), ("assistant", "官方结构回答")],
            )

    def test_deduplicates_legacy_and_completed_item_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    meta_row(),
                    user_row("重复问题", "2026-07-19T01:01:00.000Z"),
                    completed_user_item_row(
                        "重复问题", "2026-07-19T01:01:00.050Z"
                    ),
                    agent_event_row(
                        "重复回答", timestamp="2026-07-19T01:02:00.000Z"
                    ),
                    completed_agent_item_row(
                        "重复回答", timestamp="2026-07-19T01:02:00.050Z"
                    ),
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "重复问题"), ("assistant", "重复回答")],
            )

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

    def test_keeps_only_the_final_branch_after_repeated_message_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    meta_row(),
                    user_row("问题第一版", "2026-07-19T01:01:00Z"),
                    agent_event_row("第一版回答", timestamp="2026-07-19T01:02:00Z"),
                    rollback_row(1, "2026-07-19T01:03:00Z"),
                    user_row("问题第二版", "2026-07-19T01:04:00Z"),
                    agent_event_row("第二版回答", timestamp="2026-07-19T01:05:00Z"),
                    user_row("第二版之后的追问", "2026-07-19T01:06:00Z"),
                    agent_event_row("追问回答", timestamp="2026-07-19T01:07:00Z"),
                    rollback_row(2, "2026-07-19T01:08:00Z"),
                    user_row("问题最终版", "2026-07-19T01:09:00Z"),
                    agent_event_row("最终版回答", timestamp="2026-07-19T01:10:00Z"),
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [("user", "问题最终版"), ("assistant", "最终版回答")],
            )

    def test_uses_fallback_answer_per_surviving_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            write_jsonl(
                path,
                [
                    meta_row(),
                    user_row("第一问", "2026-07-19T01:01:00Z"),
                    agent_response_row("第一问的旧版回答", "2026-07-19T01:02:00Z"),
                    rollback_row(1, "2026-07-19T01:03:00Z"),
                    user_row("第一问最终版", "2026-07-19T01:04:00Z"),
                    agent_response_row("第一问最终回答", "2026-07-19T01:05:00Z"),
                    user_row("第二问", "2026-07-19T01:06:00Z"),
                    agent_event_row("第二问回答", timestamp="2026-07-19T01:07:00Z"),
                ],
            )

            conversation = exporter.parse_rollout(path)

            self.assertEqual(
                [(message.role, message.text) for message in conversation.messages],
                [
                    ("user", "第一问最终版"),
                    ("assistant", "第一问最终回答"),
                    ("user", "第二问"),
                    ("assistant", "第二问回答"),
                ],
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

    def test_export_deduplicates_identical_rollouts_by_thread_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            duplicate_rows = [
                meta_row("thread-one"),
                user_row("问题一"),
                agent_event_row("回答一"),
            ]
            write_jsonl(
                codex_home / "archived_sessions" / "thread-one.jsonl",
                duplicate_rows,
            )
            database = sqlite3.connect(codex_home / "state_1.sqlite")
            try:
                database.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, archived INTEGER)"
                )
                database.execute(
                    "INSERT INTO threads (id, title, archived) VALUES (?, ?, ?)",
                    ("thread-one", "相同标题", 0),
                )
                database.commit()
            finally:
                database.close()
            output = root / "output"

            summary = exporter.export_history(codex_home, output)

            files = list(output.rglob("*.md"))
            thread_one = [
                path
                for path in files
                if "Thread ID: `thread-one`" in path.read_text(encoding="utf-8")
            ]
            self.assertEqual(summary.discovered, 4)
            self.assertEqual(summary.exported, 2)
            self.assertEqual(summary.duplicate_rollouts, 1)
            self.assertEqual(len(files), 2)
            self.assertEqual(len(thread_one), 1)
            self.assertIn("- Archived: `false`", thread_one[0].read_text(encoding="utf-8"))

    def test_export_rejects_ambiguous_rollouts_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            write_jsonl(
                codex_home / "archived_sessions" / "thread-one.jsonl",
                [
                    meta_row("thread-one"),
                    user_row("不同分支"),
                    agent_event_row("不同回答"),
                ],
            )
            output = root / "output"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "ambiguous current rollout"):
                exporter.export_history(codex_home, output)
            self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse(output.with_name("output.__staging__").exists())

    def test_export_can_ignore_archived_conversations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            write_jsonl(
                codex_home / "archived_sessions" / "thread-three.jsonl",
                [
                    meta_row("thread-three"),
                    user_row("归档问题"),
                    agent_event_row("归档回答"),
                ],
            )
            output = root / "output"

            summary = exporter.export_history(
                codex_home, output, ignore_archived=True
            )

            files = list(output.rglob("*.md"))
            markdown = "\n".join(path.read_text(encoding="utf-8") for path in files)
            self.assertEqual(summary.discovered, 4)
            self.assertEqual(summary.exported, 2)
            self.assertEqual(summary.ignored_archived, 1)
            self.assertEqual(len(files), 2)
            self.assertNotIn("Thread ID: `thread-three`", markdown)

    def test_ignore_archived_prefers_the_database_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            database = sqlite3.connect(codex_home / "state_1.sqlite")
            try:
                database.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)"
                )
                database.execute(
                    "INSERT INTO threads (id, archived) VALUES (?, ?)",
                    ("thread-one", 1),
                )
                database.commit()
            finally:
                database.close()
            output = root / "output"

            summary = exporter.export_history(
                codex_home, output, ignore_archived=True
            )

            files = list(output.rglob("*.md"))
            markdown = "\n".join(path.read_text(encoding="utf-8") for path in files)
            self.assertEqual(summary.exported, 1)
            self.assertEqual(summary.ignored_archived, 1)
            self.assertNotIn("Thread ID: `thread-one`", markdown)
            self.assertIn("Thread ID: `thread-two`", markdown)

    def test_export_rejects_ignore_archived_for_one_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)

            with self.assertRaisesRegex(ValueError, "only available for full exports"):
                exporter.export_history(
                    codex_home,
                    root / "output",
                    session_id="thread-one",
                    ignore_archived=True,
                )

    def test_export_reports_empty_and_invalid_rollouts_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            sessions = codex_home / "sessions" / "2026" / "07" / "19"
            write_jsonl(sessions / "empty.jsonl", [meta_row("thread-empty")])
            write_jsonl(
                sessions / "invalid.jsonl",
                [user_row("缺少 session_meta")],
            )
            output = root / "output"

            summary = exporter.export_history(codex_home, output)

            self.assertEqual(summary.empty_sessions, 1)
            self.assertEqual(summary.invalid_rollouts, 1)
            self.assertEqual(summary.skipped, 2)

    def test_export_can_select_one_session_by_exact_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "selected-output"

            summary = exporter.export_history(
                codex_home, output, session_id="thread-two"
            )

            files = list(output.rglob("*.md"))
            self.assertEqual(summary.exported, 1)
            self.assertEqual(len(files), 1)
            self.assertFalse((output / "projects.toml").exists())
            markdown = files[0].read_text(encoding="utf-8")
            self.assertIn("Thread ID: `thread-two`", markdown)
            self.assertIn("问题二", markdown)
            self.assertNotIn("问题一", markdown)

    def test_unknown_session_id_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "existing-output"
            output.mkdir()
            existing = output / "keep.txt"
            existing.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "session ID not found"):
                exporter.export_history(
                    codex_home, output, session_id="missing-session"
                )

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertFalse(
                output.with_name(output.name + ".__staging__").exists()
            )

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

    def test_cli_accepts_session_id_as_a_positional_argument(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "selected-output"

            exit_code = exporter.main(
                [
                    "thread-one",
                    "--codex-home",
                    str(codex_home),
                    "--output",
                    str(output),
                ]
            )

            files = list(output.rglob("*.md"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(files), 1)
            self.assertFalse((output / "projects.toml").exists())
            self.assertIn(
                "Thread ID: `thread-one`",
                files[0].read_text(encoding="utf-8"),
            )

    def test_cli_keeps_the_named_session_id_form_for_compatibility(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "selected-output"

            exit_code = exporter.main(
                [
                    "--session-id",
                    "thread-two",
                    "--codex-home",
                    str(codex_home),
                    "--output",
                    str(output),
                ]
            )

            files = list(output.rglob("*.md"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(files), 1)
            self.assertFalse((output / "projects.toml").exists())
            self.assertIn(
                "Thread ID: `thread-two`",
                files[0].read_text(encoding="utf-8"),
            )

    def test_cli_accepts_a_codex_thread_deep_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            output = root / "selected-output"

            exit_code = exporter.main(
                [
                    "codex://threads/thread-two",
                    "--codex-home",
                    str(codex_home),
                    "--output",
                    str(output),
                ]
            )

            files = list(output.rglob("*.md"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(files), 1)
            self.assertFalse((output / "projects.toml").exists())
            self.assertIn(
                "Thread ID: `thread-two`",
                files[0].read_text(encoding="utf-8"),
            )

    def test_cli_can_ignore_archived_conversations_in_a_full_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = self.make_codex_home(root)
            write_jsonl(
                codex_home / "archived_sessions" / "thread-three.jsonl",
                [
                    meta_row("thread-three"),
                    user_row("归档问题"),
                    agent_event_row("归档回答"),
                ],
            )
            output = root / "output"

            exit_code = exporter.main(
                [
                    "--ignore-archived",
                    "--codex-home",
                    str(codex_home),
                    "--output",
                    str(output),
                ]
            )

            files = list(output.rglob("*.md"))
            markdown = "\n".join(path.read_text(encoding="utf-8") for path in files)
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(files), 2)
            self.assertNotIn("Thread ID: `thread-three`", markdown)

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


class DateFilterTests(unittest.TestCase):
    @staticmethod
    def local_timestamp(value):
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()

    def write_conversation(self, home, thread_id, created, last, extra_rows=()):
        meta = meta_row(thread_id)
        meta["payload"]["timestamp"] = self.local_timestamp(created) if created else None
        write_jsonl(
            home / "sessions" / (thread_id + ".jsonl"),
            [meta, user_row("早期问题", self.local_timestamp("2026-01-01T10:00:00")),
             agent_event_row("完整回答", timestamp=self.local_timestamp(last)), *extra_rows],
        )

    def test_until_includes_entire_day_minute_or_second(self):
        cases = [
            ("20260906", "2026-09-06T00:00:00", "2026-09-06T23:59:59.999999", "2026-09-07T00:00:00"),
            ("202609061230", "2026-09-06T12:30:00", "2026-09-06T12:30:59.999999", "2026-09-06T12:31:00"),
            ("20260906123045", "2026-09-06T12:30:45", "2026-09-06T12:30:45.999999", "2026-09-06T12:30:46"),
        ]
        for value, start, end, outside in cases:
            for field in ("created", "last_chat"):
                with self.subTest(value=value, field=field), tempfile.TemporaryDirectory() as temp:
                    home = Path(temp) / "home"
                    before = (datetime.fromisoformat(start) - timedelta(microseconds=1)).isoformat()
                    for thread_id, timestamp in (("before", before), ("start", start), ("end", end), ("outside", outside)):
                        self.write_conversation(home, thread_id, timestamp, timestamp)
                    output = Path(temp) / "output"
                    summary = exporter.export_history(
                        home, output, **{field + "_since": value, field + "_until": value}
                    )
                    self.assertEqual(summary.exported, 2)
                    self.assertEqual(summary.date_filtered, 2)
                    markdown = "\n".join(p.read_text(encoding="utf-8") for p in output.rglob("*.md"))
                    self.assertIn("Thread ID: `start`", markdown)
                    self.assertIn("Thread ID: `end`", markdown)
                    self.assertIn("早期问题", markdown)

    def test_all_four_filters_intersect_and_accept_mixed_precision(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            for thread_id, created, last in (
                ("match", "2026-08-15T12:30:00", "2026-09-06T09:30:15"),
                ("old", "2026-07-31T23:59:59", "2026-09-06T09:30:15"),
                ("new", "2026-08-16T00:00:00", "2026-09-06T09:30:15"),
                ("inactive", "2026-08-15T12:30:00", "2026-09-06T09:29:59"),
                ("later", "2026-08-15T12:30:00", "2026-09-06T09:30:16"),
            ):
                self.write_conversation(home, thread_id, created, last)
            output = Path(temp) / "output"
            result = exporter.main([
                "--codex-home", str(home), "-o", str(output),
                "--created-since", "20260801", "--created-until", "202608151230",
                "--last-chat-since", "202609060930", "--last-chat-until", "20260906093015",
            ])
            self.assertEqual(result, 0)
            files = list(output.rglob("*.md"))
            self.assertEqual(len(files), 1)
            self.assertIn("Thread ID: `match`", files[0].read_text(encoding="utf-8"))

    def test_each_filter_works_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.write_conversation(home, "early", "2026-08-01T12:00:00", "2026-09-01T12:00:00")
            self.write_conversation(home, "late", "2026-08-02T12:00:00", "2026-09-02T12:00:00")
            for name, value, expected in (
                ("created_since", "20260802", "late"),
                ("created_until", "20260801", "early"),
                ("last_chat_since", "20260902", "late"),
                ("last_chat_until", "20260901", "early"),
            ):
                with self.subTest(name=name):
                    output = Path(temp) / name
                    summary = exporter.export_history(home, output, **{name: value})
                    self.assertEqual(summary.exported, 1)
                    file = next(output.rglob("*.md"))
                    self.assertIn("Thread ID: `{}`".format(expected), file.read_text(encoding="utf-8"))

    def test_rejects_invalid_dates_and_reversed_ranges_before_output_prompt(self):
        invalid = ["2026-09-06", "202609", "2026090612", "20260230", "202609062400",
                   "20260906123060", "２０２６０９０６", " 20260906", "20260906000000Z", ""]
        cases = [["--created-since", value] for value in invalid]
        cases.extend([
            ["--created-since", "20260907", "--created-until", "20260906"],
            ["--last-chat-since", "202609061231", "--last-chat-until", "20260906123059"],
        ])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            saved = output / "keep.txt"
            saved.write_text("keep", encoding="utf-8")
            for args in cases:
                with self.subTest(args=args), mock.patch("builtins.input", side_effect=AssertionError("must validate first")):
                    self.assertEqual(exporter.main(["-o", str(output), *args]), 1)
                    self.assertEqual(saved.read_text(encoding="utf-8"), "keep")
            self.assertFalse(output.with_name("output.__staging__").exists())

    def test_single_session_ignores_invalid_and_reversed_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.write_conversation(home, "selected", "2026-08-01T12:00:00", "2026-09-01T12:00:00")
            for selector in (["selected"], ["codex://threads/selected"], ["--session-id", "selected"], ["--session-id", "codex://threads/selected"]):
                with self.subTest(selector=selector):
                    output = Path(temp) / "output"
                    result = exporter.main([
                        *selector, "--codex-home", str(home), "-o", str(output), "-f",
                        "--created-since", "invalid", "--created-until", "also-invalid",
                        "--last-chat-since", "20260907", "--last-chat-until", "20260901",
                    ])
                    self.assertEqual(result, 0)
                    self.assertEqual(len(list(output.rglob("*.md"))), 1)
                    self.assertFalse((output / "projects.toml").exists())
            summary = exporter.export_history(home, output, session_id="selected", created_since="invalid")
            self.assertEqual(summary.exported, 1)

    def test_rollbacks_and_tool_events_do_not_extend_last_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            later = self.local_timestamp("2026-09-08T12:00:00")
            self.write_conversation(home, "selected", "2026-08-01T12:00:00", "2026-09-06T12:00:00", [
                user_row("撤回的问题", later), agent_event_row("撤回的回答", timestamp=later),
                rollback_row(1, later),
                {"type": "event_msg", "timestamp": later, "payload": {"type": "token_count"}},
            ])
            output = Path(temp) / "output"
            summary = exporter.export_history(home, output, last_chat_since="20260906", last_chat_until="20260906")
            self.assertEqual(summary.exported, 1)
            markdown = next(output.rglob("*.md")).read_text(encoding="utf-8")
            self.assertNotIn("撤回", markdown)

    def test_missing_timestamps_are_counted_only_when_required(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.write_conversation(home, "unknown-created", None, "2026-09-06T12:00:00")
            write_jsonl(home / "sessions" / "unknown-chat.jsonl", [
                meta_row("unknown-chat"), user_row(timestamp=None), agent_event_row(timestamp=None),
            ])
            output = Path(temp) / "output"
            self.assertEqual(exporter.export_history(home, output).exported, 2)
            summary = exporter.export_history(home, output, created_since="20260101")
            self.assertEqual((summary.exported, summary.missing_filter_timestamps), (1, 1))
            summary = exporter.export_history(home, output, last_chat_since="20260101")
            self.assertEqual((summary.exported, summary.missing_filter_timestamps), (1, 1))

    def test_date_filters_combine_with_archiving_and_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.write_conversation(home, "selected", "2026-08-01T12:00:00", "2026-09-06T12:00:00")
            archived = home / "archived_sessions"
            archived.mkdir()
            (archived / "copy.jsonl").write_bytes((home / "sessions" / "selected.jsonl").read_bytes())
            output = Path(temp) / "output"
            summary = exporter.export_history(home, output, last_chat_since="20260906", ignore_archived=True)
            self.assertEqual((summary.exported, summary.duplicate_rollouts, summary.ignored_archived), (0, 1, 1))
            self.assertTrue((output / "projects.toml").exists())

    def test_no_matches_produces_empty_project_index(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            self.write_conversation(home, "old", "2026-08-01T12:00:00", "2026-09-01T12:00:00")
            output = Path(temp) / "output"
            summary = exporter.export_history(home, output, created_since="20260901")
            self.assertEqual((summary.exported, summary.date_filtered), (0, 1))
            self.assertEqual(list(output.rglob("*.md")), [])
            self.assertEqual((output / "projects.toml").read_text(encoding="utf-8"), "# Generated by export_codex_history.py.\n")


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
