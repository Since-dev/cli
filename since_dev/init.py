"""`since init`: the files that put Since in front of a coding agent, written into the repository so a clone carries them.

It adds its own entry and nothing else: other servers stay as they are, and a file it cannot read as JSON is left alone.
Two kinds of file: the MCP server's entry for each editor, and a rule for each coding agent, which is what gets a check made
without being asked for. Each agent reads its own file (Claude Code reads no AGENTS.md when a repository has a CLAUDE.md), so
each gets one. Nothing is written until the person says yes: it shows what it would write, then asks.
"""

import json
import re

MCP_URL = "https://api.since.dev/mcp"
TARGETS = [
    {"file": ".mcp.json", "key": "mcpServers", "entry": {"type": "http", "url": MCP_URL}, "what": "Claude Code"},
    {"file": ".cursor/mcp.json", "key": "mcpServers", "entry": {"url": MCP_URL}, "what": "Cursor"},
    {"file": ".vscode/mcp.json", "key": "servers", "entry": {"type": "http", "url": MCP_URL}, "what": "VS Code"},
]
START = "<!-- since:start -->"
END = "<!-- since:end -->"
# The rule, a paragraph to a string: what Since checks, when to call it, which tool, and what to skip. The same words wherever a coding
# agent reads them; a test holds this and the JavaScript copy to the same words.
RULE = "\n\n".join([
    "Since.dev (the `since` MCP server) checks AI model ids, dependency upgrades, Dockerfiles, GitHub Actions workflows and runtime versions for breaking changes, deprecations and retirements published after your training.",
    "Call it before you write an AI model id, add or upgrade a dependency, write or edit a Dockerfile, a compose file or a CI workflow (the images, runners, actions and runtime versions they name), set a runtime version (.nvmrc, engines, go.mod, pyproject.toml), or write code that calls an AI model or a fast-changing SDK or API; and when an error follows a version bump or began with no code change. Your training data may not include these changes, even for well-known packages such as react, next, openai or pydantic. Call it even when the version was given to you or the fix seems well known: do what was asked, and report what it finds.",
    "Which tool:\n- whats_changed: what changed in a package, model, provider, API or runtime since a version, a date or your training cutoff. With provider and current=true, the model ids to use in new code.\n- check_code: what in this code, file, model id or version is affected, on which line, and what to move to. A runtime version from any file is surface (runtime:nodejs@18); a Dockerfile, a compose file or a GitHub Actions workflow is code with its path.\n- fix_code: the fix, for a snippet or a connected repository. It runs the check itself.",
    "Skip it for code that uses no package, model, API or runtime, for general programming questions, and for the repository's own git history.",
])
BLOCK = "%s\n## Since.dev\n\n%s\n%s" % (START, RULE, END)
# A rule file of its own is the whole file. Cursor's carries the header that makes it apply in every chat.
CLAUDE_RULE = "# Since.dev\n\n%s\n" % RULE
CURSOR_RULE = "---\ndescription: Check AI model ids, dependencies, Dockerfiles, CI files and runtime versions with Since.dev\nalwaysApply: true\n---\n\n%s\n" % RULE
# Each coding agent's rule: a file of Since's own where the agent reads a folder of rules, a marked block in the file it shares with the team's own.
RULES = [
    {"file": ".claude/rules/since.md", "what": "Claude Code's rule", "whole": CLAUDE_RULE},
    {"file": ".cursor/rules/since.mdc", "what": "Cursor's rule", "whole": CURSOR_RULE},
    {"file": "GEMINI.md", "what": "Gemini CLI's rule"},
    {"file": "AGENTS.md", "what": "the rule Codex and other agents read"},
]


def _refuse_constant(name):
    raise ValueError(name)


def loads(text):
    """json.loads as JSON.parse reads it: NaN and Infinity are not JSON."""
    return json.loads(text, parse_constant=_refuse_constant)


def dumps(value, indent):
    return json.dumps(value, indent=indent, ensure_ascii=False)


def _indent_of(text):
    """The indent a JSON file already uses: its first indented line decides, two spaces when there is none."""
    match = re.search(r"^([ \t]+)\S", text, re.M)
    if not match:
        return 2
    return "\t" if "\t" in match.group(1) else len(match.group(1))


def merge_config(text, key, entry):
    """A config file with the Since entry in it. status: new, updated, same, or skipped when the file is not plain JSON."""
    if text is None or text.strip() == "":
        return {"status": "new", "text": dumps({key: {"since": entry}}, 2) + "\n"}
    try:
        data = loads(text)
    except ValueError:
        return {"status": "skipped", "text": text}
    if not isinstance(data, dict) or (key in data and not isinstance(data[key], dict)):
        return {"status": "skipped", "text": text}
    if key in data and isinstance(data[key].get("since"), dict) and data[key]["since"] == entry:
        return {"status": "same", "text": text}
    if key not in data:
        data[key] = {}
    data[key]["since"] = entry
    return {"status": "updated", "text": dumps(data, _indent_of(text)) + "\n"}


def merge_rule(text):
    """AGENTS.md with the rule between its two markers: replaced when the markers are there, added at the end when they are not."""
    if text is None or text.strip() == "":
        return {"status": "new", "text": BLOCK + "\n"}
    start = text.find(START)
    end = -1 if start < 0 else text.find(END, start)
    if start < 0 or end < 0:
        return {"status": "updated", "text": "%s\n\n%s\n" % (text.rstrip("\n"), BLOCK)}
    merged = text[:start] + BLOCK + text[end + len(END):]
    return {"status": "same", "text": text} if merged == text else {"status": "updated", "text": merged}


def merge_whole(text, whole):
    """A file that is Since's own, whole: written when it is missing, replaced when it holds an older rule."""
    if text is None or text.strip() == "":
        return {"status": "new", "text": whole}
    return {"status": "same", "text": text} if text == whole else {"status": "updated", "text": whole}


def said_yes(answer):
    """Whether an answer to a yes-or-no question is a yes. Anything else, an empty line included, is a no."""
    return bool(re.match(r"^\s*y(es)?\s*$", str(answer if answer is not None else ""), re.I))


WORDS = {
    "new": ["Wrote", "Would write"],
    "updated": ["Updated", "Would update"],
    "same": ["Unchanged", "Unchanged"],
    "skipped": ["Skipped", "Skipped"],
}


def init(io, program, dry_run=False, yes=False):
    root = io.root()
    rows = [dict(target, result=merge_config(io.read_file("%s/%s" % (root, target["file"])), target["key"], target["entry"])) for target in TARGETS]
    for rule in RULES:
        text = io.read_file("%s/%s" % (root, rule["file"]))
        rows.append(dict(rule, result=merge_whole(text, rule["whole"]) if "whole" in rule else merge_rule(text)))
    changes = [row for row in rows if row["result"]["status"] in ("new", "updated")]
    skipped = [row for row in rows if row["result"]["status"] == "skipped"]
    width = max(len(row["file"]) for row in rows) + 2

    def listing(wrote):
        """The list of files, as what would happen or as what did."""
        return [""] + ["  %s%s%s" % (WORDS[row["result"]["status"]][0 if wrote else 1].ljust(14), row["file"].ljust(width), row["what"]) for row in rows] + [""]

    by_hand = []
    for row in skipped:
        by_hand.append('  %s is not plain JSON, so it was left alone. Add this under "%s" by hand:' % (row["file"], row["key"]))
        by_hand.append('    "since": %s' % json.dumps(row["entry"], separators=(",", ":"), ensure_ascii=False))
        by_hand.append("")
    them = "it" if len(changes) == 1 else "them"

    def say(lines):
        io.out("\n".join(lines) + "\n")

    after = ["  Commit these files: everyone who clones this repository gets the same setup.", "  The first tool call opens a sign-in in the browser. No key goes in any file."]

    if not changes:
        say(listing(True) + by_hand + ["  Everything is already set up."])
        return 0
    # Nothing is written before a yes: --yes on the command line, or the answer to the question. With nobody to ask, it says how.
    if yes and not dry_run:
        for row in changes:
            io.write_file("%s/%s" % (root, row["file"]), row["result"]["text"], None)
        say(listing(True) + by_hand + after)
        return 0
    say(listing(False) + by_hand)
    if dry_run:
        say(["  Nothing was written. Run %s init to write %s." % (program, them)])
        return 0
    if not io.tty:
        say(["  Nothing was written: there is nobody to ask here. Run %s init --yes to write %s." % (program, them)])
        return 0
    if not said_yes(io.prompt("  Write %s? [y/N] " % ("this file" if len(changes) == 1 else "these %d files" % len(changes)))):
        say(["  Nothing was written."])
        return 0
    for row in changes:
        io.write_file("%s/%s" % (root, row["file"]), row["result"]["text"], None)
    say(["", "  Wrote %s." % ("it" if len(changes) == 1 else "%d files" % len(changes))] + after)
    return 0
