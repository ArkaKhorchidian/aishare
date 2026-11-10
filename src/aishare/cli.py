"""aishare: how much of the code you shipped did AI write?"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, agentlogs
from .share import NAMES, AgentIndex, Summary, analyze_repo, find_repos, identities

COLORS = {"claude": "#d97757", "codex": "#10a37f", "gemini": "#4285f4", "copilot": "#a371f7", "cursor": "#e6e6e6",
          "aider": "#14b014", "devin": "#3b82f6", "windsurf": "#09b6a2", "amp": "#f34e3f", "you": "#5c6370"}


def bar(parts: list[tuple[str, int]], total: int, width: int = 48) -> Text:
    t = Text()
    used = 0
    for key, n in parts:
        w = round(width * n / total) if total else 0
        w = min(w, width - used)
        if n and w == 0:
            w = 1
        t.append("█" * w, COLORS.get(key, "white"))
        used += w
    if used < width:
        t.append("█" * (width - used), COLORS["you"])
    return t


def render(s: Summary, console: Console, top: int = 8) -> None:
    if not s.total:
        console.print(Text("No lines of yours from that period are still in these repos. "
                           "Try a wider window with --since, or point it at a folder of repos.", "yellow"))
        return
    head = Text.assemble((f"{s.share:.0%}", "bold #d97757"), " of the code you shipped ", s.period(),
                         " was written by ", ("AI", "bold"), ".")
    parts: list = [head, Text(f"{s.ai:,} of {s.total:,} lines still in your code, across {len(s.repos)} "
                              f"repo{'s' * (len(s.repos) != 1)}", "dim"), Text("")]
    agents = s.by_agent.most_common()
    parts.append(bar(agents, s.total))
    legend = Text()
    for a, n in agents:
        legend.append("■ ", COLORS.get(a, "white"))
        legend.append(f"{NAMES.get(a, a)} {n / s.total:.0%}   ")
    legend.append("■ ", COLORS["you"])
    legend.append(f"You {1 - s.share:.0%}")
    parts += [legend, Text("")]

    months = s.months()
    if len(months) > 1:
        g = Table.grid(padding=(0, 1))
        g.add_column(no_wrap=True, style="dim")
        g.add_column()
        g.add_column(justify="right", style="dim")
        for m, c in months.items():
            tot = sum(c.values())
            ai = tot - c.get("you", 0)
            label = datetime.strptime(m, "%Y-%m").strftime("%b")
            g.add_row(label, bar([(a, n) for a, n in c.most_common() if a != "you"], tot, 30),
                      f"{ai / tot:.0%}" if tot else "")
        parts += [Text("Month by month", "bold"), g, Text("")]

    repos = sorted((r for r in s.repos if r.total), key=lambda r: -r.total)
    if repos:
        t = Table(box=None, header_style="dim", padding=(0, 2))
        t.add_column("Repo", no_wrap=True)
        t.add_column("Your lines", justify="right")
        t.add_column("AI", justify="right")
        t.add_column("Mostly", no_wrap=True)
        for r in repos[:top]:
            main = r.by_agent.most_common(1)
            t.add_row(Text(r.repo.name, "bold"), f"{r.total:,}", f"{r.ai / r.total:.0%}",
                      Text(NAMES.get(main[0][0], main[0][0]), COLORS.get(main[0][0], "")) if main else Text("—", "dim"))
        if len(repos) > top:
            t.add_row(Text(f"+ {len(repos) - top} more", "dim"), "", "", "")
        parts += [t, Text("")]

    via_logs = sum(r.via_logs for r in s.repos)
    via_tr = sum(r.via_trailers for r in s.repos)
    how = []
    if via_logs:
        how.append(f"{via_logs:,} lines matched to agent session logs")
    if via_tr:
        how.append(f"{via_tr:,} from commits with an AI co-author")
    if how:
        parts.append(Text("How we know: " + "; ".join(how) + ".", "dim"))
    if not s.log_events:
        parts.append(Text("No Claude Code, Codex or Gemini CLI logs found here, so only commit co-author trailers "
                          "were counted.", "dim"))
    console.print(Panel(Group(*parts), title="[bold]Who wrote your code?[/]", border_style="#d97757", padding=(1, 2)))
    console.print(Text("  Share: ", "dim") + Text(s.share_line(), "italic"))
    console.print()


def parse_date(s: str) -> float:
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return time.mktime(datetime.strptime(s, fmt).timetuple())
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"not a date: {s} (use YYYY, YYYY-MM or YYYY-MM-DD)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aishare", description="How much of the code you shipped did AI write? "
                                 "Matches git blame against your AI agents' session logs and AI co-author trailers.")
    ap.add_argument("paths", nargs="*", default=["."], help="repos, or folders to search for repos (default: .)")
    ap.add_argument("--since", type=parse_date, help="start of the window (default: Jan 1 this year)")
    ap.add_argument("--until", type=parse_date, help="end of the window (default: now)")
    ap.add_argument("--email", action="append", default=[], help="another email or name you commit as (repeatable)")
    ap.add_argument("--top", type=int, default=8, help="rows in the repo table")
    ap.add_argument("--json", action="store_true", help="print numbers as JSON")
    ap.add_argument("-V", "--version", action="version", version=f"aishare {__version__}")
    a = ap.parse_args(argv)
    console = Console(stderr=a.json)

    now = time.time()
    since = a.since or time.mktime((time.localtime(now).tm_year, 1, 1, 0, 0, 0, 0, 0, -1))
    until = a.until or now
    me = identities(a.email)
    if not me:
        console.print("[yellow]Couldn't read your git identity.[/] Set it with git config --global user.email, "
                      "or pass --email you@example.com")
        return 2
    repos = find_repos([Path(p) for p in a.paths])
    if not repos:
        console.print("[yellow]No git repos found there.[/]")
        return 2

    index = AgentIndex()
    seen = {}
    with console.status("Reading agent session logs…") if not a.json else _null():
        for ev in agentlogs.all_events(since=since - 86400):
            index.add(ev)
            seen[ev.agent] = seen.get(ev.agent, 0) + 1
    results = []
    with console.status(f"Running git blame across {len(repos)} repo(s)…") if not a.json else _null():
        for r in repos:
            results.append(analyze_repo(r, me, since, until, index))
    s = Summary(results, since, until, log_events=index.events)
    if a.json:
        print(json.dumps({
            "since": since, "until": until, "lines": s.total, "ai_lines": s.ai, "share": round(s.share, 4),
            "by_agent": dict(s.by_agent), "months": {m: dict(c) for m, c in s.months().items()},
            "repos": [{"repo": str(r.repo), "lines": r.total, "ai_lines": r.ai, "by_agent": dict(r.by_agent),
                       "via_logs": r.via_logs, "via_trailers": r.via_trailers} for r in results],
        }, indent=2))
    else:
        render(s, console, a.top)
    return 0


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    sys.exit(main())
