"""Build a made-up developer's year (3 repos, Claude Code + Codex + Gemini CLI logs, a few Copilot-co-authored
commits) in a temp folder and run aishare on it. Used to record the README GIF; nothing here is real data."""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

random.seed(11)
ROOT = Path(tempfile.mkdtemp(prefix="aishare-demo-"))
HOME = ROOT / "home"
ME = ("Sam Rivera", "sam@example.com")
VERBS = ["load", "save", "parse", "render", "fetch", "build", "update", "sync", "validate", "format", "merge", "cache"]
NOUNS = ["user", "order", "invoice", "session", "config", "report", "cart", "token", "event", "profile", "price"]


def code_block(n_funcs: int) -> str:
    out = []
    for _ in range(n_funcs):
        v, nn = random.choice(VERBS), random.choice(NOUNS)
        k = random.randint(100, 999)
        out.append(f"def {v}_{nn}_{k}(client, {nn}_id):\n"
                   f"    record = client.get('{nn}s', {nn}_id)\n"
                   f"    result_{k} = {{'id': {nn}_id, 'status': record.status}}\n"
                   f"    return result_{k}\n")
    return "\n".join(out)


def git(repo, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME=ME[0], GIT_AUTHOR_EMAIL=ME[1], GIT_COMMITTER_NAME=ME[0],
               GIT_COMMITTER_EMAIL=ME[1], HOME=str(HOME))
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


claude_lines, codex_n = {}, 0
year = time.localtime().tm_year
HOME.mkdir(parents=True)
subprocess.run(["git", "config", "--file", str(HOME / ".gitconfig"), "user.email", ME[1]], check=True)
subprocess.run(["git", "config", "--file", str(HOME / ".gitconfig"), "user.name", ME[0]], check=True)
code = ROOT / "code"
for repo_name, weight in (("storefront", 1.0), ("pricing-api", 0.8), ("ops-scripts", 0.4)):
    repo = code / repo_name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for month in range(1, 11):
        t = time.mktime((year, month, 10, 15, 0, 0, 0, 0, -1))
        ai_ratio = min(0.8, 0.12 + 0.065 * month)  # AI's share creeps up through the year
        for k in range(int(3 * weight) + 1):
            fname = f"src/{repo_name.replace('-', '_')}_{month:02d}_{k}.py"
            path = repo / fname
            path.parent.mkdir(parents=True, exist_ok=True)
            when = t + k * 3600
            if random.random() < ai_ratio:
                agent = random.choices(["claude", "codex", "gemini", "copilot"], [0.6, 0.25, 0.08, 0.07])[0]
                body = code_block(random.randint(3, 6))
                msg = f"add {fname}"
                if agent == "claude":
                    d = HOME / ".claude" / "projects" / f"-{repo_name}"
                    d.mkdir(parents=True, exist_ok=True)
                    with (d / f"s{month}.jsonl").open("a") as fh:
                        fh.write(json.dumps({"type": "assistant", "timestamp": iso(when - 300), "cwd": str(repo),
                                             "sessionId": f"s{month}", "message": {"content": [
                                                 {"type": "tool_use", "name": "Write", "input": {
                                                     "file_path": str(path), "content": body}}]}}) + "\n")
                elif agent == "codex":
                    d = HOME / ".codex" / "sessions" / str(year) / f"{month:02d}" / "10"
                    d.mkdir(parents=True, exist_ok=True)
                    codex_n += 1
                    patch = f"*** Begin Patch\n*** Add File: {fname}\n" + \
                        "".join("+" + l + "\n" for l in body.splitlines()) + "*** End Patch\n"
                    (d / f"rollout-{year}-{month:02d}-10T00-00-00-{codex_n}.jsonl").write_text(
                        json.dumps({"timestamp": iso(when - 300), "type": "session_meta",
                                    "payload": {"id": str(codex_n), "cwd": str(repo)}}) + "\n" +
                        json.dumps({"timestamp": iso(when - 200), "type": "response_item",
                                    "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}}) + "\n")
                elif agent == "gemini":
                    d = HOME / ".gemini" / "tmp" / repo_name / "chats"
                    d.mkdir(parents=True, exist_ok=True)
                    (HOME / ".gemini" / "projects.json").write_text(json.dumps({"projects": {
                        str(code / n): n for n in ("storefront", "pricing-api", "ops-scripts")}}))
                    with (d / "session-1.jsonl").open("a") as fh:
                        fh.write(json.dumps({"id": f"m{month}{k}", "type": "gemini", "timestamp": iso(when - 300),
                                             "content": "", "toolCalls": [{"id": "c", "name": "write_file",
                                                                           "args": {"file_path": str(path),
                                                                                    "content": body}}]}) + "\n")
                else:
                    msg += "\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>"
            else:
                body, msg = code_block(random.randint(3, 6)), f"handwritten {fname}"
            path.write_text(body)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", msg, when=when)

env = dict(os.environ, HOME=str(HOME), USERPROFILE=str(HOME))
env.pop("CLAUDE_CONFIG_DIR", None)
env.pop("CODEX_HOME", None)
os.chdir(code)
sys.exit(subprocess.call(["aishare", "."], env=env))
