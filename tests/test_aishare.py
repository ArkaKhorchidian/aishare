import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from aishare import agentlogs, cli
from aishare.share import AgentIndex, analyze_repo, norm, trailer_agent

DAY = 86400
T0 = time.mktime((2026, 3, 2, 12, 0, 0, 0, 0, -1))
ME = "me@example.com"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def git(repo, *args, when=None, author=("Me", ME)):
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=author[0],
               GIT_COMMITTER_EMAIL=author[1], GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def commit(repo, files: dict, msg: str, when: float, author=("Me", ME)):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg, when=when, author=author)


HUMAN = "def load_config(path):\n    with open(path) as fh:\n        return json.load(fh)\n"
CLAUDE = "def save_config(path, data):\n    with open(path, 'w') as fh:\n        json.dump(data, fh, indent=2)\n"
CODEX = "def parse_args(argv):\n    parser = argparse.ArgumentParser()\n    return parser.parse_args(argv)\n"
COPILOT = "class Cache:\n    def __init__(self):\n        self.items = {}\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    # 1. you write some code
    commit(repo, {"app.py": HUMAN}, "load config", T0)
    # 2. Claude rewrites app.py (keeping your lines) and adds save_config; you commit it
    claude_dir = home / ".claude" / "projects" / "-proj"
    claude_dir.mkdir(parents=True)
    lines = [
        {"type": "user", "timestamp": iso(T0 + DAY), "cwd": str(repo), "sessionId": "s1",
         "message": {"role": "user", "content": "add a save function"}},
        {"type": "assistant", "timestamp": iso(T0 + DAY + 5), "cwd": str(repo), "sessionId": "s1",
         "message": {"role": "assistant", "model": "claude-x", "usage": {"input_tokens": 100, "output_tokens": 50},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": str(repo / "app.py"), "content": HUMAN + CLAUDE}},
                                 {"type": "tool_use", "id": "t2", "name": "Bash",
                                  "input": {"command": "pytest -q"}}]}},
        {"type": "user", "timestamp": iso(T0 + DAY + 6), "sessionId": "s1", "toolUseResult": {},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    ]
    (claude_dir / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    commit(repo, {"app.py": HUMAN + CLAUDE}, "save config", T0 + DAY + 600)
    # 3. Codex adds cli.py with apply_patch
    codex_dir = home / ".codex" / "sessions" / "2026" / "03" / "05"
    codex_dir.mkdir(parents=True)
    patch = "*** Begin Patch\n*** Add File: cli.py\n" + "".join("+" + l + "\n" for l in CODEX.splitlines()) + \
        "*** End Patch\n"
    rollout = [
        {"timestamp": iso(T0 + 3 * DAY), "type": "session_meta", "payload": {"id": "c1", "cwd": str(repo)}},
        {"timestamp": iso(T0 + 3 * DAY), "type": "event_msg", "payload": {"type": "user_message", "message": "add cli"}},
        {"timestamp": iso(T0 + 3 * DAY + 2), "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "x", "input": patch}},
        {"timestamp": iso(T0 + 3 * DAY + 3), "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "y",
                     "arguments": json.dumps({"cmd": "python cli.py --help"})}},
        {"timestamp": iso(T0 + 3 * DAY + 4), "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "z",
                     "arguments": json.dumps({"command": ["bash", "-lc", "ls -la"]})}},
    ]
    (codex_dir / "rollout-2026-03-05T12-00-00-c1.jsonl").write_text("\n".join(json.dumps(x) for x in rollout) + "\n")
    commit(repo, {"cli.py": CODEX}, "cli", T0 + 3 * DAY + 900)
    # 4. a commit with a Copilot co-author trailer
    commit(repo, {"cache.py": COPILOT}, "cache\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
           T0 + 5 * DAY)
    # 5. someone else's code doesn't count
    commit(repo, {"theirs.py": "x_value = compute_other_thing()\n"}, "theirs", T0 + 6 * DAY, author=("Bob", "bob@x"))
    # 6. Gemini session that touched another project (shouldn't match anything here)
    gem = home / ".gemini" / "tmp" / "other" / "chats"
    gem.mkdir(parents=True)
    (home / ".gemini" / "projects.json").write_text(json.dumps({"projects": {"/elsewhere": "other"}}))
    (gem / "session-1.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"sessionId": "g1", "projectHash": "h", "startTime": iso(T0)},
        {"id": "m1", "type": "user", "timestamp": iso(T0), "content": [{"text": "hi"}]},
        {"id": "m2", "type": "gemini", "timestamp": iso(T0 + 1), "content": "", "model": "gemini-x",
         "tokens": {"total": 70}, "toolCalls": [{"id": "c", "name": "run_shell_command", "args": {"command": "ls"},
                                                 "status": "success", "timestamp": iso(T0 + 1)}]},
        {"id": "m2", "toolCalls": [{"id": "c", "result": [{"text": "out"}]}]},
    ]) + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return repo, home


def test_norm():
    assert norm("    }") is None and norm("") is None and norm("  return  x  ") == "return x"


def test_trailers():
    assert trailer_agent("feat\n\nCo-Authored-By: Claude Opus <noreply@anthropic.com>") == "claude"
    assert trailer_agent("x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)") == "claude"
    assert trailer_agent("x\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>") == "cursor"
    assert trailer_agent("x\n\nCo-authored-by: Jane Doe <jane@x.com>") is None
    assert trailer_agent("Mention Claude in the subject only") is None


def test_agent_logs(world):
    events = list(agentlogs.all_events())
    kinds = {(e.agent, e.kind) for e in events}
    assert {("claude", "prompt"), ("claude", "write"), ("claude", "command"), ("codex", "prompt"),
            ("codex", "edit"), ("codex", "command"), ("gemini", "prompt"), ("gemini", "command")} <= kinds
    cmds = sorted(e.command for e in events if e.kind == "command")
    assert cmds == ["ls", "ls -la", "pytest -q", "python cli.py --help"]
    gem = [e for e in events if e.agent == "gemini" and e.kind == "command"][0]
    assert gem.cwd == "/elsewhere" and gem.tokens == 70
    claude_tokens = sum(e.tokens for e in events if e.agent == "claude")
    assert claude_tokens == 150
    # tool results are not prompts
    assert [e.text for e in events if e.agent == "claude" and e.kind == "prompt"] == ["add a save function"]


def test_analyze(world):
    repo, _ = world
    index = AgentIndex()
    for ev in agentlogs.all_events():
        index.add(ev)
    r = analyze_repo(repo, {ME}, T0 - DAY, T0 + 30 * DAY, index)
    # 3 human + 3 claude + 3 codex + 3 copilot lines are yours; Bob's line isn't
    assert r.total == 12
    assert dict(r.by_agent) == {"claude": 3, "codex": 3, "copilot": 3}
    assert r.via_logs == 6 and r.via_trailers == 3
    assert r.files["app.py"] == 3 and r.file_totals["app.py"] == 6
    # lines you wrote BEFORE Claude rewrote the file aren't credited to Claude
    r2 = analyze_repo(repo, {ME}, T0 - DAY, T0 + 1, index)
    assert r2.total == 3 and r2.ai == 0


def test_cli(world, capsys):
    repo, _ = world
    assert cli.main([str(repo), "--since", "2026-01-01", "--until", "2026-12-31", "--email", ME, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lines"] == 12 and out["ai_lines"] == 9 and out["share"] == 0.75


def test_render(world):
    from rich.console import Console
    from aishare.share import Summary
    repo, _ = world
    index = AgentIndex()
    for ev in agentlogs.all_events():
        index.add(ev)
    s = Summary([analyze_repo(repo, {ME}, T0 - DAY, T0 + 30 * DAY, index)], T0 - DAY, T0 + 30 * DAY,
                log_events=index.events)
    c = Console(width=100, record=True, file=io.StringIO())
    cli.render(s, c)
    out = c.export_text()
    assert "75% of the code you shipped" in out and "Claude 25%" in out and "uvx aishare" in out
    assert "(Claude 25%, Codex 25%, Copilot 25%)" in s.share_line()
