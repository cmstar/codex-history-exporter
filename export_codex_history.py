#!/usr/bin/env python3
"""Export local Codex conversations to project-grouped Markdown files."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


INVALID_COMPONENT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    text: str
    timestamp: datetime
    sequence: int


@dataclass(frozen=True)
class ParsedConversation:
    thread_id: str
    cwd: Optional[str]
    source_path: Path
    messages: Tuple[ChatMessage, ...]
    last_timestamp: datetime
    archived: bool


@dataclass(frozen=True)
class ResolvedProject:
    name: str
    path: Optional[str]


@dataclass(frozen=True)
class ExportSummary:
    discovered: int
    exported: int
    excluded_subagents: int
    skipped: int
    malformed_lines: int
    output_root: Path


@dataclass(frozen=True)
class _ParseOutcome:
    conversation: Optional[ParsedConversation]
    status: str
    malformed_lines: int


@dataclass
class _ConversationTurn:
    user_message: ChatMessage
    final_answers: List[ChatMessage]
    fallback_answers: List[ChatMessage]


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_text(content: object, expected_type: str) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != expected_type:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def _is_subagent(meta: dict) -> bool:
    if meta.get("thread_source") == "subagent":
        return True
    source = meta.get("source")
    return isinstance(source, dict) and "subagent" in source


def _parse_rollout_with_status(
    path: Path, session_id: Optional[str] = None
) -> _ParseOutcome:
    meta = None
    turns: List[_ConversationTurn] = []
    malformed_lines = 0
    sequence = 0

    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return _ParseOutcome(None, "invalid", 0)

    with stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                print(
                    "warning: malformed JSONL line {} in {}".format(line_number, path),
                    file=sys.stderr,
                )
                continue
            if not isinstance(row, dict):
                continue
            row_type = row.get("type")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row_type == "session_meta" and meta is None:
                meta = payload
                if session_id is not None and meta.get("id") != session_id:
                    return _ParseOutcome(None, "filtered", 0)
                continue

            if row_type == "event_msg" and payload.get("type") == "thread_rolled_back":
                num_turns = payload.get("num_turns")
                if (
                    isinstance(num_turns, int)
                    and not isinstance(num_turns, bool)
                    and num_turns > 0
                ):
                    del turns[max(0, len(turns) - num_turns) :]
                continue

            timestamp = _parse_timestamp(row.get("timestamp"))
            if timestamp is None:
                timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

            if row_type == "event_msg" and payload.get("type") == "user_message":
                text = payload.get("message")
                if isinstance(text, str) and text.strip():
                    turns.append(
                        _ConversationTurn(
                            ChatMessage("user", text, timestamp, sequence),
                            [],
                            [],
                        )
                    )
                    sequence += 1
                continue

            if (
                row_type == "event_msg"
                and payload.get("type") == "agent_message"
                and payload.get("phase") == "final_answer"
            ):
                text = payload.get("message")
                if turns and isinstance(text, str) and text.strip():
                    turns[-1].final_answers.append(
                        ChatMessage("assistant", text, timestamp, sequence)
                    )
                    sequence += 1
                continue

            if (
                row_type == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
            ):
                text = _message_text(payload.get("content"), "output_text")
                if turns and text:
                    turns[-1].fallback_answers.append(
                        ChatMessage("assistant", text, timestamp, sequence)
                    )
                    sequence += 1

    if not isinstance(meta, dict):
        if session_id is not None:
            return _ParseOutcome(None, "filtered", 0)
        return _ParseOutcome(None, "invalid", malformed_lines)
    if _is_subagent(meta):
        return _ParseOutcome(None, "subagent", malformed_lines)

    thread_id = meta.get("id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return _ParseOutcome(None, "invalid", malformed_lines)

    visible_messages: List[ChatMessage] = []
    for turn in turns:
        visible_messages.append(turn.user_message)
        visible_messages.extend(turn.final_answers or turn.fallback_answers)
    visible_messages.sort(key=lambda message: (message.timestamp, message.sequence))

    meta_timestamp = _parse_timestamp(meta.get("timestamp"))
    if visible_messages:
        last_timestamp = max(message.timestamp for message in visible_messages)
    elif meta_timestamp is not None:
        last_timestamp = meta_timestamp
    else:
        last_timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    conversation = ParsedConversation(
        thread_id=thread_id,
        cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
        source_path=path,
        messages=tuple(visible_messages),
        last_timestamp=last_timestamp,
        archived="archived_sessions" in path.parts,
    )
    status = "ok" if visible_messages else "empty"
    return _ParseOutcome(conversation, status, malformed_lines)


def parse_rollout(path: Path) -> Optional[ParsedConversation]:
    """Parse one rollout, returning None for invalid or sub-agent histories."""
    return _parse_rollout_with_status(Path(path)).conversation


def _state_database_sort_key(path: Path) -> int:
    match = re.search(r"state_(\d+)\.sqlite$", path.name)
    return int(match.group(1)) if match else -1


def _load_database_titles(codex_home: Path) -> Dict[str, str]:
    titles: Dict[str, str] = {}
    databases = sorted(
        codex_home.glob("state_*.sqlite"), key=_state_database_sort_key, reverse=True
    )
    if not databases:
        return titles
    database_path = databases[0].resolve()
    try:
        connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if not {"id", "title"}.issubset(columns):
                return titles
            for thread_id, title in connection.execute(
                "SELECT id, title FROM threads WHERE title IS NOT NULL AND title != ''"
            ):
                if isinstance(thread_id, str) and isinstance(title, str) and title.strip():
                    titles[thread_id] = title.strip()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        print("warning: cannot read {}: {}".format(database_path, error), file=sys.stderr)
    return titles


def load_titles(codex_home: Path) -> Dict[str, str]:
    """Load thread titles, with the session index taking precedence over SQLite."""
    codex_home = Path(codex_home)
    titles = _load_database_titles(codex_home)
    newest: Dict[str, Tuple[datetime, int, str]] = {}
    index_path = codex_home / "session_index.jsonl"
    if not index_path.is_file():
        return titles
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as stream:
            for sequence, line in enumerate(stream):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                thread_id = row.get("id")
                title = row.get("thread_name")
                if not isinstance(thread_id, str) or not isinstance(title, str):
                    continue
                if not title.strip():
                    continue
                updated = _parse_timestamp(row.get("updated_at")) or datetime.min.replace(
                    tzinfo=timezone.utc
                )
                candidate = (updated, sequence, title.strip())
                if thread_id not in newest or candidate[:2] >= newest[thread_id][:2]:
                    newest[thread_id] = candidate
    except OSError as error:
        print("warning: cannot read {}: {}".format(index_path, error), file=sys.stderr)
    titles.update({thread_id: value[2] for thread_id, value in newest.items()})
    return titles


def _normalize_path(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip() or value.strip() == "~":
        return None
    normalized = os.path.expandvars(os.path.expanduser(value.strip()))
    normalized = normalized.replace("\\", "/")
    if normalized.startswith("//?/"):
        normalized = normalized[4:]
    normalized = re.sub(r"/+", "/", normalized).rstrip("/")
    return normalized.casefold() or None


def _is_under_path(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + "/")


def _path_name(value: str) -> str:
    stripped = value.rstrip("/\\")
    if "\\" in stripped or re.match(r"^[A-Za-z]:", stripped):
        return ntpath.basename(stripped)
    return Path(stripped).name


class ProjectResolver:
    def __init__(
        self,
        projects: Dict[str, dict],
        assignments: Dict[str, dict],
        projectless_ids: Iterable[str],
    ) -> None:
        self.projects = projects
        self.assignments = assignments
        self.projectless_ids = set(projectless_ids)
        roots = []
        self.project_roots: Dict[str, List[Tuple[str, str]]] = {}
        for project_id, project in projects.items():
            if not isinstance(project, dict):
                continue
            name = project.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            for root_path in project.get("rootPaths", []):
                normalized = _normalize_path(root_path)
                if normalized:
                    display_path = root_path.strip()
                    roots.append(
                        (normalized, name.strip(), project_id, display_path)
                    )
                    self.project_roots.setdefault(project_id, []).append(
                        (normalized, display_path)
                    )
        self.roots = sorted(roots, key=lambda item: len(item[0]), reverse=True)
        for project_roots in self.project_roots.values():
            project_roots.sort(key=lambda item: len(item[0]), reverse=True)

    @classmethod
    def from_codex_home(cls, codex_home: Path) -> "ProjectResolver":
        state_path = Path(codex_home) / ".codex-global-state.json"
        state = {}
        if state_path.is_file():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
            except (OSError, json.JSONDecodeError) as error:
                print("warning: cannot read {}: {}".format(state_path, error), file=sys.stderr)
        projects = state.get("local-projects", {})
        assignments = state.get("thread-project-assignments", {})
        projectless_ids = state.get("projectless-thread-ids", [])
        return cls(
            projects if isinstance(projects, dict) else {},
            assignments if isinstance(assignments, dict) else {},
            projectless_ids if isinstance(projectless_ids, list) else [],
        )

    def _configured_root(
        self, project_id: str, normalized_cwd: Optional[str]
    ) -> Optional[str]:
        roots = self.project_roots.get(project_id, [])
        if normalized_cwd is not None:
            for normalized_root, display_path in roots:
                if _is_under_path(normalized_cwd, normalized_root):
                    return display_path
        return roots[0][1] if roots else None

    def resolve_with_path(self, conversation: ParsedConversation) -> ResolvedProject:
        cwd = _normalize_path(conversation.cwd)
        assignment = self.assignments.get(conversation.thread_id)
        if isinstance(assignment, dict) and assignment.get("projectKind") == "local":
            project_id = assignment.get("projectId")
            project = self.projects.get(project_id)
            if isinstance(project, dict):
                name = project.get("name")
                if isinstance(name, str) and name.strip():
                    return ResolvedProject(
                        name.strip(),
                        self._configured_root(project_id, cwd)
                        if isinstance(project_id, str)
                        else None,
                    )

        if conversation.thread_id in self.projectless_ids:
            return ResolvedProject("chat", None)

        if cwd is None:
            return ResolvedProject("chat", None)
        for root, name, _project_id, display_path in self.roots:
            if _is_under_path(cwd, root):
                return ResolvedProject(name, display_path)

        home = _normalize_path(str(Path.home()))
        if home is not None and cwd == home:
            return ResolvedProject("chat", None)
        name = _path_name(conversation.cwd or "")
        if not name.strip():
            return ResolvedProject("chat", None)
        return ResolvedProject(name.strip(), (conversation.cwd or "").strip())

    def resolve(self, conversation: ParsedConversation) -> str:
        return self.resolve_with_path(conversation).name


def safe_component(value: str, fallback: str, limit: int = 120) -> str:
    """Return a portable Windows-safe filename or directory component."""
    cleaned = INVALID_COMPONENT_CHARS.sub("_", value or "")
    cleaned = re.sub(r"[\r\n\t]+", "_", cleaned)
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    if WINDOWS_RESERVED_NAMES.match(cleaned):
        cleaned = "_" + cleaned
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(". ")
    return cleaned or fallback


def _metadata_value(value: object) -> str:
    return str(value).replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def render_markdown(
    conversation: ParsedConversation, title: str, project: str
) -> str:
    local_last = conversation.last_timestamp.astimezone()
    lines = [
        "# {}".format(title),
        "",
        "- Thread ID: `{}`".format(_metadata_value(conversation.thread_id)),
        "- Project: `{}`".format(_metadata_value(project)),
        "- Last chat: `{}`".format(local_last.isoformat(timespec="seconds")),
    ]
    if conversation.cwd:
        lines.append("- Working directory: `{}`".format(_metadata_value(conversation.cwd)))
    lines.append("- Archived: `{}`".format(str(conversation.archived).lower()))

    for message in conversation.messages:
        lines.extend(
            [
                "",
                "## User" if message.role == "user" else "## Codex",
                "",
                message.text.rstrip(),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _toml_string(value: str) -> str:
    if "'''" not in value and "\r" not in value and "\n" not in value:
        return "'''{}'''".format(value)
    return json.dumps(value, ensure_ascii=False)


def render_project_index(project_paths: Dict[str, Iterable[str]]) -> str:
    """Render project working directories as deterministic, valid TOML."""
    lines = ["# Generated by export_codex_history.py."]
    for project in sorted(project_paths, key=str.casefold):
        paths = sorted(set(project_paths[project]), key=str.casefold)
        if not paths or project == "chat":
            continue
        lines.extend(["", "[{}]".format(json.dumps(project, ensure_ascii=False))])
        if len(paths) == 1:
            lines.append("Path = {}".format(_toml_string(paths[0])))
        else:
            lines.append(
                "Paths = [{}]".format(
                    ", ".join(_toml_string(path) for path in paths)
                )
            )
    return "\n".join(lines) + "\n"


def _conversation_title(conversation: ParsedConversation, titles: Dict[str, str]) -> str:
    title = titles.get(conversation.thread_id)
    if title and title.strip():
        return title.strip()
    for message in conversation.messages:
        if message.role == "user" and message.text.strip():
            first_line = next(
                (line.strip() for line in message.text.splitlines() if line.strip()), ""
            )
            if first_line:
                return first_line
    return conversation.thread_id


def _discover_rollouts(codex_home: Path) -> List[Path]:
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    if not any(root.is_dir() for root in roots):
        raise FileNotFoundError(
            "no sessions or archived_sessions directory under {}".format(codex_home)
        )
    paths = []
    for root in roots:
        if root.is_dir():
            paths.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    return sorted(paths)


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("refusing to replace symbolic-link path: {}".format(path))


def _absolute_without_resolving(path: Path) -> Path:
    """Make a path absolute without following a symlink or junction target."""
    return Path(os.path.abspath(os.fspath(path)))


def _remove_private_tree(path: Path) -> None:
    _assert_not_symlink(path)
    if path.exists():
        if not path.is_dir():
            raise RuntimeError("expected directory but found file: {}".format(path))
        shutil.rmtree(path)


def _allocate_output_path(
    staging: Path,
    project: str,
    conversation: ParsedConversation,
    title: str,
    allocated: set,
) -> Path:
    folder = safe_component(project, "chat")
    timestamp = conversation.last_timestamp.astimezone().strftime("%Y%m%d%H%M%S")
    safe_title = safe_component(title, conversation.thread_id)
    filename = "{}__{}.md".format(timestamp, safe_title)
    key = (folder + "/" + filename).casefold()
    if key in allocated:
        short_id = re.sub(r"[^A-Za-z0-9]", "", conversation.thread_id)[-8:] or "thread"
        filename = "{}__{}__{}.md".format(timestamp, safe_title, short_id)
        key = (folder + "/" + filename).casefold()
        number = 2
        while key in allocated:
            filename = "{}__{}__{}-{}.md".format(
                timestamp, safe_title, short_id, number
            )
            key = (folder + "/" + filename).casefold()
            number += 1
    allocated.add(key)
    return staging / folder / filename


def _clear_directory_contents(path: Path) -> None:
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _publish_in_place_with_rollback(
    staging: Path, output_root: Path, backup: Path
) -> None:
    shutil.copytree(output_root, backup, symlinks=True)
    try:
        _clear_directory_contents(output_root)
        for child in list(staging.iterdir()):
            child.rename(output_root / child.name)
        staging.rmdir()
    except Exception:
        _clear_directory_contents(output_root)
        shutil.copytree(backup, output_root, dirs_exist_ok=True, symlinks=True)
        raise
    finally:
        if backup.exists() and not backup.is_symlink():
            shutil.rmtree(backup)


def _publish_staging(staging: Path, output_root: Path, backup: Path) -> None:
    _assert_not_symlink(output_root)
    _assert_not_symlink(staging)
    _assert_not_symlink(backup)
    _remove_private_tree(backup)
    had_output = output_root.exists()
    if had_output:
        if not output_root.is_dir():
            raise RuntimeError("output path is not a directory: {}".format(output_root))
        try:
            output_root.rename(backup)
        except PermissionError:
            _publish_in_place_with_rollback(staging, output_root, backup)
            return
    try:
        staging.rename(output_root)
    except Exception:
        if had_output and backup.exists() and not output_root.exists():
            backup.rename(output_root)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def export_history(
    codex_home: Path, output_root: Path, session_id: Optional[str] = None
) -> ExportSummary:
    codex_home = Path(codex_home).expanduser().resolve()
    output_root = _absolute_without_resolving(Path(output_root).expanduser())
    if not codex_home.is_dir():
        raise FileNotFoundError("Codex home does not exist: {}".format(codex_home))
    if session_id is not None:
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session ID cannot be empty")

    rollouts = _discover_rollouts(codex_home)
    titles = load_titles(codex_home)
    resolver = ProjectResolver.from_codex_home(codex_home)
    staging = output_root.with_name(output_root.name + ".__staging__")
    backup = output_root.with_name(output_root.name + ".__backup__")
    _remove_private_tree(staging)
    staging.mkdir(parents=True)

    exported = 0
    excluded_subagents = 0
    skipped = 0
    malformed_lines = 0
    matched_rollouts = 0
    allocated = set()
    project_paths: Dict[str, Set[str]] = {}
    try:
        for rollout in rollouts:
            outcome = _parse_rollout_with_status(rollout, session_id)
            if outcome.status == "filtered":
                continue
            if session_id is not None:
                matched_rollouts += 1
            malformed_lines += outcome.malformed_lines
            if outcome.status == "subagent":
                excluded_subagents += 1
                continue
            conversation = outcome.conversation
            if conversation is None or outcome.status != "ok":
                skipped += 1
                continue
            title = _conversation_title(conversation, titles)
            resolved_project = resolver.resolve_with_path(conversation)
            project = resolved_project.name
            if project != "chat" and resolved_project.path:
                project_paths.setdefault(project, set()).add(resolved_project.path)
            target = _allocate_output_path(
                staging, project, conversation, title, allocated
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(render_markdown(conversation, title, project))
            exported += 1
        if session_id is not None and exported == 0:
            if matched_rollouts == 0:
                raise RuntimeError("session ID not found: {}".format(session_id))
            if excluded_subagents:
                raise RuntimeError(
                    "session ID belongs to a sub-agent and cannot be exported: {}".format(
                        session_id
                    )
                )
            raise RuntimeError(
                "session has no exportable messages: {}".format(session_id)
            )
        if session_id is None:
            with (staging / "projects.toml").open(
                "w", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(render_project_index(project_paths))
        _publish_staging(staging, output_root, backup)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise

    return ExportSummary(
        discovered=len(rollouts),
        exported=exported,
        excluded_subagents=excluded_subagents,
        skipped=skipped,
        malformed_lines=malformed_lines,
        output_root=output_root,
    )


def _default_codex_home(argument: Optional[str]) -> Path:
    if argument:
        return Path(argument)
    environment_home = os.environ.get("CODEX_HOME")
    if environment_home:
        return Path(environment_home)
    return Path.home() / ".codex"


def _confirm_output_replacement(output_root: Path, force: bool) -> bool:
    if output_root.is_symlink():
        raise RuntimeError(
            "refusing to replace symbolic-link path: {}".format(output_root)
        )
    if not output_root.exists():
        return True
    if not output_root.is_dir():
        raise RuntimeError("output path is not a directory: {}".format(output_root))
    if not any(output_root.iterdir()) or force:
        return True
    try:
        answer = input(
            "Output directory '{}' is not empty. Overwrite its contents? [y/N]: ".format(
                output_root
            )
        )
    except EOFError:
        answer = ""
    return answer.strip().casefold() in {"y", "yes"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export local Codex conversations as project-grouped Markdown."
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        metavar="SESSION_ID",
        help="export only the main session whose ID exactly matches SESSION_ID",
    )
    parser.add_argument(
        "--codex-home",
        help="Codex data directory (default: CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output directory (default: ./output in the current working directory)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace a nonempty output directory without prompting",
    )
    parser.add_argument(
        "--session-id",
        dest="named_session_id",
        metavar="ID",
        help="named form of the optional SESSION_ID positional argument",
    )
    arguments = parser.parse_args(argv)
    if arguments.session_id is not None and arguments.named_session_id is not None:
        parser.error("SESSION_ID and --session-id cannot be used together")
    session_id = arguments.named_session_id or arguments.session_id
    try:
        output_root = _absolute_without_resolving(
            Path(arguments.output).expanduser()
            if arguments.output
            else Path.cwd() / "output"
        )
        if not _confirm_output_replacement(output_root, arguments.force):
            print("Export cancelled. Existing output was not changed.")
            return 0
        summary = export_history(
            _default_codex_home(arguments.codex_home),
            output_root,
            session_id=session_id,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 1
    print("Codex history export complete")
    print("  discovered: {}".format(summary.discovered))
    print("  exported: {}".format(summary.exported))
    print("  sub-agents excluded: {}".format(summary.excluded_subagents))
    print("  skipped: {}".format(summary.skipped))
    print("  malformed JSONL lines: {}".format(summary.malformed_lines))
    print("  output: {}".format(summary.output_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
