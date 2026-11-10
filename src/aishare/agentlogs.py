"""Read the session logs that AI coding agents keep on your machine, into one common shape.

Supported (all read-only, nothing leaves your machine):
- Claude Code:  ~/.claude/projects/<project>/<session>.jsonl (and subagent logs under it)
- Codex CLI:    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
- Gemini CLI:   ~/.gemini/tmp/<project>/chats/session-*.json(l), with ~/.gemini/projects.json for folders
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

AGENTS = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI"}


@dataclass
class Event:
    agent: str  # "claude" | "codex" | "gemini"
    session: str
    ts: float | None
    cwd: str | None
    kind: str  # "prompt" | "command" | "read" | "write" | "edit" | "fetch" | "tool"
    tool: str = ""
    command: str = ""  # shell command line
    path: str = ""  # file read/written
    added: list[str] = field(default_factory=list)  # lines the agent wrote
    text: str = ""  # prompt text, URL, etc.
    model: str = ""
    tokens: int = 0  # tokens used by this turn, where the log says


def parse_ts(s) -> float | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e11 else 1)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _lines(text: str) -> list[str]:
    return text.splitlines() if isinstance(text, str) else []


def _jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    yield d
    except OSError:
        return


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------
def claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _prompt_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_events(path: Path) -> Iterator[Event]:
    for d in _jsonl(path):
        typ = d.get("type")
        if typ not in ("user", "assistant"):
            continue
        ts, cwd, sid = parse_ts(d.get("timestamp")), d.get("cwd"), d.get("sessionId") or path.stem
        msg = d.get("message") or {}
        content = msg.get("content")
        if typ == "user":
            if d.get("isMeta") or d.get("isCompactSummary") or d.get("toolUseResult") is not None:
                continue
            text = _prompt_text(content)
            if text and not text.startswith(("<command-", "<local-command", "Caveat:", "[Request interrupted")) \
                    and not (isinstance(content, list) and any(b.get("type") == "tool_result" for b in content
                                                               if isinstance(b, dict))):
                yield Event("claude", sid, ts, cwd, "prompt", text=text)
            continue
        usage = msg.get("usage") or {}
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
        model = msg.get("model") or ""
        first = True
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name") or "", b.get("input") or {}
            ev = Event("claude", sid, ts, cwd, "tool", tool=name, model=model, tokens=tokens if first else 0)
            first = False
            if name == "Bash":
                ev.kind, ev.command = "command", str(inp.get("command") or "")
            elif name in ("Read", "NotebookRead"):
                ev.kind, ev.path = "read", str(inp.get("file_path") or inp.get("notebook_path") or "")
            elif name == "Write":
                ev.kind, ev.path, ev.added = "write", str(inp.get("file_path") or ""), _lines(inp.get("content", ""))
            elif name == "Edit":
                ev.kind, ev.path, ev.added = "edit", str(inp.get("file_path") or ""), _lines(inp.get("new_string", ""))
            elif name == "MultiEdit":
                ev.kind, ev.path = "edit", str(inp.get("file_path") or "")
                for e in inp.get("edits") or []:
                    ev.added += _lines((e or {}).get("new_string", ""))
            elif name == "NotebookEdit":
                ev.kind, ev.path, ev.added = "edit", str(inp.get("notebook_path") or ""), _lines(inp.get("new_source", ""))
            elif name in ("WebFetch", "WebSearch"):
                ev.kind, ev.text = "fetch", str(inp.get("url") or inp.get("query") or "")
            elif name in ("Grep", "Glob", "LS"):
                ev.kind, ev.path = "read", str(inp.get("path") or "")
            yield ev
        if first and tokens:  # a turn with no tool calls still used tokens
            yield Event("claude", sid, ts, cwd, "tool", tool="", model=model, tokens=tokens)


# ---------------------------------------------------------------------------
# Codex CLI
# ---------------------------------------------------------------------------
def codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def parse_apply_patch(patch: str) -> list[tuple[str, list[str]]]:
    """[(path, added lines)] from Codex's *** Begin Patch format."""
    out: list[tuple[str, list[str]]] = []
    for line in patch.splitlines():
        m = PATCH_FILE.match(line)
        if m:
            out.append((m.group(2).strip(), []))
        elif out and line.startswith("+") and not line.startswith("+++"):
            out[-1][1].append(line[1:])
    return out


def _codex_command(name: str, args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args if name in ("shell_command",) else ""
    if not isinstance(args, dict):
        return ""
    cmd = args.get("cmd", args.get("command"))
    if isinstance(cmd, list):
        # ["bash", "-lc", "real command"] -> "real command"
        if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") and os.path.basename(str(cmd[0])) in ("bash", "sh", "zsh"):
            return str(cmd[2])
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def codex_events(path: Path) -> Iterator[Event]:
    sid, cwd, model = path.stem.replace("rollout-", ""), None, ""
    last_total = 0
    for d in _jsonl(path):
        typ, p = d.get("type"), d.get("payload") or {}
        ts = parse_ts(d.get("timestamp"))
        if typ == "session_meta":
            sid = str(p.get("id") or p.get("session_id") or sid)
            cwd = p.get("cwd") or cwd
            continue
        if typ == "turn_context":
            cwd = p.get("cwd") or cwd
            model = p.get("model") or model
            continue
        if typ == "event_msg":
            if p.get("type") == "user_message" and p.get("message"):
                yield Event("codex", sid, ts, cwd, "prompt", text=str(p["message"]))
            elif p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                total = (p["info"].get("total_token_usage") or {}).get("total_tokens") or 0
                if total > last_total:
                    yield Event("codex", sid, ts, cwd, "tool", model=model, tokens=int(total - last_total))
                    last_total = total
            continue
        if typ != "response_item":
            continue
        ptype, name = p.get("type"), p.get("name") or ""
        if ptype == "local_shell_call":
            cmd = _codex_command("shell", (p.get("action") or {}))
            yield Event("codex", sid, ts, cwd, "command", tool="local_shell", command=cmd, model=model)
        elif ptype in ("function_call", "custom_tool_call"):
            raw = p.get("arguments") if ptype == "function_call" else p.get("input")
            if name == "apply_patch":
                patch = raw
                if ptype == "function_call":
                    try:
                        patch = json.loads(raw).get("input", "")
                    except (ValueError, AttributeError):
                        patch = ""
                for fpath, added in parse_apply_patch(patch or ""):
                    yield Event("codex", sid, ts, cwd, "edit", tool="apply_patch", path=fpath, added=added, model=model)
            elif name in ("shell", "exec_command", "shell_command", "container.exec"):
                cmd = _codex_command(name, raw)
                if "apply_patch" in cmd and "*** Begin Patch" in cmd:  # older Codex sent patches through the shell
                    for fpath, added in parse_apply_patch(cmd):
                        yield Event("codex", sid, ts, cwd, "edit", tool="apply_patch", path=fpath, added=added,
                                    model=model)
                else:
                    yield Event("codex", sid, ts, cwd, "command", tool=name, command=cmd, model=model)
            elif name in ("web_search", "web_fetch"):
                yield Event("codex", sid, ts, cwd, "fetch", tool=name, text=str(raw)[:300], model=model)
            else:
                yield Event("codex", sid, ts, cwd, "tool", tool=name, model=model)


# ---------------------------------------------------------------------------
# Gemini CLI
# ---------------------------------------------------------------------------
def gemini_home() -> Path:
    return Path.home() / ".gemini"


def gemini_projects() -> dict[str, str]:
    """shortId -> project folder, from ~/.gemini/projects.json."""
    for base in (gemini_home(), Path.home() / ".cache" / ".gemini"):
        try:
            data = json.loads((base / "projects.json").read_text())
            return {v: k for k, v in (data.get("projects") or {}).items()}
        except (OSError, ValueError, AttributeError):
            continue
    return {}


def _gemini_records(path: Path) -> tuple[dict, list[dict]]:
    """(metadata, messages) for both the old single-JSON and the newer JSONL session files."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return {}, []
        return data, list(data.get("messages") or [])
    meta: dict = {}
    msgs: dict[str, dict] = {}
    order: list[str] = []
    for d in _jsonl(path):
        if "sessionId" in d and "projectHash" in d and "type" not in d:
            meta.update(d)
        elif "$set" in d:
            meta.update(d["$set"] if isinstance(d["$set"], dict) else {})
        elif isinstance(d.get("id"), str):
            if d["id"] in msgs:  # a later patch of the same message (e.g. tool results filled in)
                cur = msgs[d["id"]]
                for k, v in d.items():
                    if k == "toolCalls" and isinstance(v, list):
                        known = {t.get("id"): t for t in cur.get("toolCalls") or []}
                        for t in v:
                            known.setdefault(t.get("id"), {}).update(t)
                        cur["toolCalls"] = list(known.values())
                    elif k != "$patch":
                        cur[k] = v
            else:
                msgs[d["id"]] = dict(d)
                order.append(d["id"])
    return meta, [msgs[i] for i in order]


def gemini_events(path: Path, projects: dict[str, str] | None = None) -> Iterator[Event]:
    meta, messages = _gemini_records(path)
    sid = str(meta.get("sessionId") or path.stem)
    short = path.parent.parent.name if path.parent.name == "chats" else ""
    cwd = (projects or {}).get(short)
    for m in messages:
        ts = parse_ts(m.get("timestamp"))
        if m.get("type") == "user":
            text = m.get("content")
            if isinstance(text, list):
                text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict))
            if text:
                yield Event("gemini", sid, ts, cwd, "prompt", text=str(text))
            continue
        if m.get("type") != "gemini":
            continue
        tok = m.get("tokens") or {}
        tokens = int(tok.get("total") or 0) if isinstance(tok, dict) else 0
        model = m.get("model") or ""
        calls = m.get("toolCalls") or []
        for i, tc in enumerate(calls):
            name, args = tc.get("name") or "", tc.get("args") or {}
            ev = Event("gemini", sid, parse_ts(tc.get("timestamp")) or ts, cwd, "tool", tool=name, model=model,
                       tokens=tokens if i == 0 else 0)
            if name in ("run_shell_command", "ShellTool"):
                ev.kind, ev.command = "command", str(args.get("command") or "")
                if args.get("dir_path") and not cwd:
                    ev.cwd = str(args["dir_path"])
            elif name == "write_file":
                ev.kind, ev.path, ev.added = "write", str(args.get("file_path") or ""), _lines(args.get("content", ""))
            elif name in ("replace", "edit"):
                ev.kind, ev.path, ev.added = "edit", str(args.get("file_path") or ""), _lines(args.get("new_string", ""))
            elif name in ("read_file", "read_many_files", "glob", "search_file_content", "grep_search", "list_directory"):
                ev.kind = "read"
                ev.path = str(args.get("file_path") or args.get("absolute_path") or args.get("path") or
                              args.get("dir_path") or "")
            elif name in ("web_fetch", "google_web_search"):
                ev.kind, ev.text = "fetch", str(args.get("url") or args.get("prompt") or args.get("query") or "")[:300]
            yield ev
        if not calls and tokens:
            yield Event("gemini", sid, ts, cwd, "tool", model=model, tokens=tokens)


# ---------------------------------------------------------------------------
# Everything
# ---------------------------------------------------------------------------
def session_files(roots: dict[str, Path] | None = None) -> list[tuple[str, Path]]:
    roots = roots or {"claude": claude_root(), "codex": codex_root(), "gemini": gemini_home() / "tmp"}
    out: list[tuple[str, Path]] = []
    if roots.get("claude") and roots["claude"].is_dir():
        out += [("claude", p) for p in sorted(roots["claude"].rglob("*.jsonl"))]
    if roots.get("codex") and roots["codex"].is_dir():
        out += [("codex", p) for p in sorted(roots["codex"].rglob("rollout-*.jsonl"))]
    if roots.get("gemini") and roots["gemini"].is_dir():
        out += [("gemini", p) for p in sorted(roots["gemini"].glob("*/chats/session-*.json*"))]
    return out


def all_events(roots: dict[str, Path] | None = None, since: float | None = None) -> Iterator[Event]:
    projects = gemini_projects()
    for agent, path in session_files(roots):
        try:
            if since and path.stat().st_mtime < since:
                continue  # untouched since before the window: nothing in it can be newer
        except OSError:
            continue
        if agent == "claude":
            it = claude_events(path)
        elif agent == "codex":
            it = codex_events(path)
        else:
            it = gemini_events(path, projects)
        for ev in it:
            if since and ev.ts and ev.ts < since:
                continue
            yield ev
