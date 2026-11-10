"""Who wrote the code you shipped? Match the lines git blames on you against what your AI agents wrote."""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .agentlogs import Event

# Commit trailers and bodies that name an AI tool. (agent key, pattern)
TRAILER_AGENTS = [
    ("claude", re.compile(r"noreply@anthropic\.com|\bclaude\b|claude\.ai/code|claude\.com/claude-code", re.I)),
    ("codex", re.compile(r"\bcodex\b|openai\.com", re.I)),
    ("copilot", re.compile(r"\bcopilot\b|copilot-swe-agent", re.I)),
    ("cursor", re.compile(r"cursoragent@cursor\.com|\bcursor agent\b|\bcursor\b", re.I)),
    ("gemini", re.compile(r"\bgemini\b|gemini-code-assist|google-labs-jules|\bjules\b", re.I)),
    ("aider", re.compile(r"\baider\b", re.I)),
    ("devin", re.compile(r"\bdevin\b|devin-ai-integration", re.I)),
    ("windsurf", re.compile(r"\bwindsurf\b|\bcodeium\b|\bcascade\b", re.I)),
    ("amp", re.compile(r"ampcode\.com", re.I)),
]
AI_LINE = re.compile(r"^(co-authored-by|generated[- ]by|generated with|assisted-by|ai-assisted):", re.I)
GENERATED_WITH = re.compile(r"generated with \[?(claude code|codex|cursor|gemini|aider|copilot)", re.I)
BOT_AUTHORS = re.compile(r"copilot-swe-agent|devin-ai-integration|google-labs-jules|cursor.*agent|claude\[bot\]|"
                         r"codex\[bot\]|openai-codex", re.I)

NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "copilot": "Copilot", "cursor": "Cursor",
         "aider": "Aider", "devin": "Devin", "windsurf": "Windsurf", "amp": "Amp"}


def norm(line: str) -> str | None:
    """Whitespace-insensitive form of a line, or None for lines too trivial to attribute (braces, blanks)."""
    s = re.sub(r"\s+", " ", line.strip())
    if len(s) < 4 or not re.search(r"[A-Za-z0-9]{2}", s):
        return None
    return s


def git(repo: Path, *args: str, check: bool = False) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                           timeout=300)
    except (OSError, subprocess.SubprocessError):
        return ""
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout


def find_repos(paths: list[Path], depth: int = 4) -> list[Path]:
    out: list[Path] = []
    skip = {"node_modules", ".venv", "venv", "vendor", "target", "dist", "build", "Library", ".cache", ".Trash"}

    def walk(d: Path, level: int):
        if (d / ".git").exists():
            out.append(d)
            return
        if level >= depth:
            return
        try:
            for c in sorted(d.iterdir()):
                if c.is_dir() and not c.is_symlink() and not c.name.startswith(".") and c.name not in skip:
                    walk(c, level + 1)
        except OSError:
            pass

    for p in paths:
        p = p.expanduser().resolve()
        top = git(p, "rev-parse", "--show-toplevel").strip()
        if top:
            out.append(Path(top))
        else:
            walk(p, 0)
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def identities(extra: list[str] = ()) -> set[str]:
    ids = {e.lower() for e in extra}
    for key in ("user.email", "user.name"):
        try:
            v = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()
        except OSError:
            v = ""
        if v:
            ids.add(v.lower())
    return ids


def trailer_agent(message: str) -> str | None:
    for line in message.splitlines():
        line = line.strip()
        if AI_LINE.match(line) or GENERATED_WITH.search(line):
            for key, rx in TRAILER_AGENTS:
                if rx.search(line):
                    return key
    return None


@dataclass
class AgentLine:
    agent: str
    ts: float


class AgentIndex:
    """Lines each agent wrote, per absolute file path, with when it wrote them."""

    def __init__(self):
        self.by_file: dict[str, dict[str, list[AgentLine]]] = defaultdict(lambda: defaultdict(list))
        self.events = 0

    def add(self, ev: Event) -> None:
        if ev.kind not in ("write", "edit") or not ev.path or not ev.added:
            return
        p = ev.path
        if not os.path.isabs(p):
            if not ev.cwd:
                return
            p = os.path.join(ev.cwd, p)
        p = os.path.normpath(p)
        self.events += 1
        bucket = self.by_file[p]
        for line in ev.added:
            n = norm(line)
            if n:
                bucket[n].append(AgentLine(ev.agent, ev.ts or 0.0))

    def lookup(self, path: str, line: str, committed: float, slack: float = 120) -> str | None:
        """The agent that wrote this exact line in this file before it was committed, if any."""
        n = norm(line)
        if not n:
            return None
        hits = self.by_file.get(os.path.normpath(path), {}).get(n)
        if not hits:
            return None
        before = [h for h in hits if h.ts <= committed + slack]
        return before[-1].agent if before else None


@dataclass
class RepoResult:
    repo: Path
    total: int = 0  # substantive lines you shipped in the window that are still in the code
    by_agent: Counter = field(default_factory=Counter)  # agent -> lines
    by_month: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))  # "2026-03" -> {"you":n, agent:n}
    files: Counter = field(default_factory=Counter)  # file -> AI lines
    file_totals: Counter = field(default_factory=Counter)
    via_logs: int = 0
    via_trailers: int = 0

    @property
    def ai(self) -> int:
        return sum(self.by_agent.values())


def blame_file(repo: Path, rel: str):
    """Yield (sha, author_mail, author_name, author_time, line) for every line at HEAD."""
    out = git(repo, "blame", "--line-porcelain", "-w", "HEAD", "--", rel)
    sha = mail = name = None
    t = 0
    for line in out.splitlines():
        if line.startswith("\t"):
            yield sha, mail, name, t, line[1:]
            continue
        head, _, rest = line.partition(" ")
        if re.fullmatch(r"[0-9a-f]{40}", head):
            sha = head
        elif head == "author-mail":
            mail = rest.strip("<>").lower()
        elif head == "author":
            name = rest.lower()
        elif head == "author-time":
            t = int(rest)


def analyze_repo(repo: Path, me: set[str], since: float, until: float, index: AgentIndex) -> RepoResult:
    res = RepoResult(repo)
    if not git(repo, "rev-parse", "--verify", "-q", "HEAD").strip():
        return res
    # which commits name an AI tool, and which are by agent bots
    log = git(repo, "log", "--all", f"--since=@{int(since)}", f"--until=@{int(until)}", "--format=%H%x00%an%x00%ae%x00%B%x1e")
    ai_commit: dict[str, str] = {}
    bot_commit: set[str] = set()
    for rec in log.split("\x1e"):
        parts = rec.strip("\n").split("\x00")
        if len(parts) < 4:
            continue
        sha, an, ae, body = parts[0], parts[1], parts[2], parts[3]
        agent = trailer_agent(body)
        if BOT_AUTHORS.search(f"{an} {ae}"):
            bot_commit.add(sha)
            agent = agent or next((k for k, rx in TRAILER_AGENTS if rx.search(f"{an} {ae}")), "copilot")
        if agent:
            ai_commit[sha] = agent
    # files touched in the window that still exist
    names = git(repo, "log", f"--since=@{int(since)}", f"--until=@{int(until)}", "--name-only", "--format=", "HEAD")
    tracked = set(git(repo, "ls-files").splitlines())
    files = sorted({n for n in names.splitlines() if n and n in tracked})
    for rel in files:
        if is_generated(rel):
            continue
        abspath = str(repo / rel)
        for sha, mail, name, t, line in blame_file(repo, rel):
            if not (since <= t <= until) or not norm(line):
                continue
            mine = (mail in me or name in me)
            if not mine and sha not in bot_commit:
                continue
            res.total += 1
            res.file_totals[rel] += 1
            month = _month(t)
            agent = index.lookup(abspath, line, t)
            if agent:
                res.via_logs += 1
            elif sha in ai_commit:
                agent = ai_commit[sha]
                res.via_trailers += 1
            if agent:
                res.by_agent[agent] += 1
                res.files[rel] += 1
                res.by_month[month][agent] += 1
            else:
                res.by_month[month]["you"] += 1
    return res


GENERATED = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock|"
                       r"go\.sum|.*\.min\.(js|css)|.*\.map|.*\.svg|.*\.ipynb|.*\.snap|.*\.lock)$|(^|/)(dist|build|vendor|"
                       r"node_modules|__snapshots__)/")


def is_generated(rel: str) -> bool:
    return bool(GENERATED.search(rel))


def _month(t: float) -> str:
    import time
    return time.strftime("%Y-%m", time.localtime(t))


@dataclass
class Summary:
    repos: list[RepoResult]
    since: float
    until: float
    log_events: int = 0
    agents_seen: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(r.total for r in self.repos)

    @property
    def by_agent(self) -> Counter:
        c = Counter()
        for r in self.repos:
            c.update(r.by_agent)
        return Counter(dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))))  # stable order for ties

    @property
    def ai(self) -> int:
        return sum(self.by_agent.values())

    @property
    def share(self) -> float:
        return self.ai / self.total if self.total else 0.0

    def months(self) -> dict[str, Counter]:
        out: dict[str, Counter] = defaultdict(Counter)
        for r in self.repos:
            for m, c in r.by_month.items():
                out[m].update(c)
        return dict(sorted(out.items()))

    def period(self) -> str:
        import time
        y0, y1 = time.localtime(self.since).tm_year, time.localtime(self.until).tm_year
        jan1 = time.mktime((y0, 1, 1, 0, 0, 0, 0, 0, -1))
        if y0 == y1 and abs(self.since - jan1) < 86400:
            return f"in {y0}"
        t = time.localtime(self.since)
        return f"since {time.strftime('%b', t)} {t.tm_mday}, {t.tm_year}"

    def share_line(self) -> str:
        top = ", ".join(f"{NAMES.get(a, a)} {n / self.total:.0%}" for a, n in self.by_agent.most_common(3)) \
            if self.total else ""
        s = f"{self.share:.0%} of the code I shipped {self.period()} was written by AI"
        return s + (f" ({top})" if top and self.ai else "") + ". How much of yours? uvx aishare"
