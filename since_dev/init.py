"""`since init`: the files that put Since in front of a coding agent, written into the repository so a clone carries them.

It adds its own entry and nothing else: other servers stay as they are, and a file it cannot read as JSON is left alone.
"""

import json
import re

MCP_URL = "https://api.since.dev/mcp"
TARGETS = [
    {"file": ".mcp.json", "key": "mcpServers", "entry": {"type": "http", "url": MCP_URL}, "what": "Claude Code"},
    {"file": ".cursor/mcp.json", "key": "mcpServers", "entry": {"url": MCP_URL}, "what": "Cursor"},
    {"file": ".vscode/mcp.json", "key": "servers", "entry": {"type": "http", "url": MCP_URL}, "what": "VS Code"},
]
RULE_FILE = "AGENTS.md"
START = "<!-- since:start -->"
END = "<!-- since:end -->"
# The rule every listing ships; a test holds this and the JavaScript copy to the same words.
RULE = "\n".join([
    "When you change a dependency version, a manifest or lockfile, an AI model ID, a runtime version, a Docker base image or a CI runner label, check it with the Since.dev MCP tools first:",
    "",
    "- `check_code` with the manifest, lockfile, model ID or `surface` (`runtime:<product>@<version>`) reports what is affected, on which line, the days left and the replacement.",
    "- `whats_changed` with the package and `since` set to the current version lists every breaking change and deprecation between that version and today, each with its source.",
    "",
    "Treat `needs_review` and `unknown` as not settled, never as safe. Use the replacement the answer names. `fix_code` opens a fix pull request for a connected repository, and the editor asks before it runs.",
])
BLOCK = "%s\n## Since.dev\n\n%s\n%s" % (START, RULE, END)


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


WORDS = {
    "new": ["Wrote", "Would write"],
    "updated": ["Updated", "Would update"],
    "same": ["Unchanged", "Unchanged"],
    "skipped": ["Skipped", "Skipped"],
}


def init(io, program, dry_run):
    root = io.root()
    lines = [""]
    skipped = []
    changed = 0
    rows = [dict(target, result=merge_config(io.read_file("%s/%s" % (root, target["file"])), target["key"], target["entry"])) for target in TARGETS]
    rows.append({"file": RULE_FILE, "what": "the rule every coding agent reads", "result": merge_rule(io.read_file("%s/%s" % (root, RULE_FILE)))})
    for row in rows:
        status = row["result"]["status"]
        if status in ("new", "updated"):
            changed += 1
            if not dry_run:
                io.write_file("%s/%s" % (root, row["file"]), row["result"]["text"], None)
        if status == "skipped":
            skipped.append(row)
        lines.append("  %s%s%s" % (WORDS[status][1 if dry_run else 0].ljust(14), row["file"].ljust(20), row["what"]))
    lines.append("")
    for row in skipped:
        lines.append('  %s is not plain JSON, so it was left alone. Add this under "%s" by hand:' % (row["file"], row["key"]))
        lines.append('    "since": %s' % json.dumps(row["entry"], separators=(",", ":"), ensure_ascii=False))
        lines.append("")
    if dry_run:
        lines.append("  Nothing was written. Run %s init to write %s." % (program, "it" if changed == 1 else "them") if changed else "  Everything is already set up.")
    elif changed:
        lines.append("  Commit these files: everyone who clones this repository gets the same setup.")
        lines.append("  The first tool call opens a sign-in in the browser. No key goes in any file.")
    else:
        lines.append("  Everything is already set up.")
    lines.append("")
    io.out("\n".join(lines))
    return 0
