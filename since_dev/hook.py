"""The write-time hook (Feature 5): Claude Code runs `since hook post-tool-use` after each file its agent writes. It checks the file
as it now stands, as one set of files, and tells the agent only what is proven on the lines it just wrote, in a short note. A file that
names no model, version, runtime or package is never sent, nor one unchanged since its last check, and the same note is never said
twice in a session. It answers within three seconds or stays silent: a slow or unreachable Since never holds the agent up longer."""

import hashlib
import json
import re

HOOK_TIMEOUT_MS = 3000
"""How long the hook waits for Since: past it, nothing is said and the agent goes on. A file whose model id was just written is read
closely by a model before Since answers (about a second on its own), so the wait is three seconds, not one."""
MAX_CHARACTERS = 500000

NAMES_SOMETHING = re.compile(
    r"\b(?:import|require|from|using|use)\b|\b(?:gpt-|o[134]-|claude-|gemini-|mistral|llama|command-r|text-embedding|dall-e|whisper|deepseek)"
    r"|^\s*FROM\s|\b(?:runs-on|uses|node-version|python-version|go-version|image)\s*:", re.I | re.M)
DECLARES = re.compile(
    r"(?:^|/)(?:package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json|pom\.xml|build\.gradle(?:\.kts)?"
    r"|[^/]+\.csproj|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|Dockerfile[^/]*|docker-compose[^/]*\.ya?ml"
    r"|compose[^/]*\.ya?ml|\.nvmrc|\.python-version|\.tool-versions)$", re.I)


def names_something(path, content):
    """Whether a file is worth sending: one that names a model, version, runtime or package."""
    return bool(DECLARES.search(path)) or bool(NAMES_SOMETHING.search(content))


def _lines_of(content, piece, every):
    lines = set()
    if not piece:
        return lines
    start = 0
    while True:
        at = content.find(piece, start)
        if at < 0:
            break
        first = content[:at].count("\n") + 1
        for line in range(first, first + piece.count("\n") + 1):
            lines.add(line)
        if not every:
            break
        start = at + max(1, len(piece))
    return lines


def written(event, read_file):
    """What the agent just wrote: the file's path inside the project, its text now, and the lines written (None for every line)."""
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    file = tool_input.get("file_path") if isinstance(tool_input.get("file_path"), str) else None
    cwd = event.get("cwd").rstrip("/") if isinstance(event.get("cwd"), str) else None
    if not file or not cwd:
        return None
    path = file[len(cwd) + 1:] if file.startswith(cwd + "/") else None if file.startswith("/") else file
    if not path or any(part in ("..", ".git", "node_modules") for part in path.split("/")):
        return None
    if event.get("tool_name") == "Write" and isinstance(tool_input.get("content"), str):
        return {"path": path, "content": tool_input["content"], "lines": None}
    content = read_file(file if file.startswith("/") else "%s/%s" % (cwd, file))
    if content is None:
        return None
    if isinstance(tool_input.get("edits"), list):
        pieces = [(edit.get("new_string"), bool(edit.get("replace_all"))) for edit in tool_input["edits"] if isinstance(edit, dict)]
    else:
        pieces = [(tool_input.get("new_string"), bool(tool_input.get("replace_all")))]
    lines = set()
    for text, every in pieces:
        if isinstance(text, str):
            lines |= _lines_of(content, text, every)
    return {"path": path, "content": content, "lines": lines}


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _note_of(finding, place):
    reason = re.sub(r"\.?$", ".", str(finding.get("reason") or "").strip(), count=1)
    replacement = finding.get("replacement")
    use = " Use %s." % replacement if replacement and str(replacement).lower() not in reason.lower() else ""
    edit = finding.get("edit") if isinstance(finding.get("edit"), dict) else {}
    write = " Write: %s" % edit["to"].strip() if isinstance(edit.get("to"), str) and edit["to"].strip() else ""
    return "%s:%s: %s%s%s" % (place["file"], place["line"], reason, use, write)


def post_tool_use(ctx, event, state, check):
    """The PostToolUse hook. `state` is the session's memory (each file's last checked text and the notes said). Returns the note for
    the agent, or None to say nothing."""
    wrote = written(event, ctx["io"].read_file)
    if not wrote or not wrote["content"].strip() or len(wrote["content"]) > MAX_CHARACTERS or not names_something(wrote["path"], wrote["content"]):
        return None
    seen = _hash(wrote["content"])
    if state["files"].get(wrote["path"]) == seen:
        return None
    # Every file written that names something, answered or not: the finish check reads them all.
    state["written"] = list(dict.fromkeys((state.get("written") or []) + [wrote["path"]]))
    answer = check({"files": [{"path": wrote["path"], "content": wrote["content"]}], "detail": "summary"})
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("findings"), list):
        return None
    state["files"][wrote["path"]] = seen
    notes = []
    for finding in answer["json"]["findings"]:
        if not isinstance(finding, dict) or finding.get("state") != "affected":
            continue
        for place in finding.get("where") if isinstance(finding.get("where"), list) else []:
            if not isinstance(place, dict) or place.get("file") != wrote["path"] or not isinstance(place.get("line"), int):
                continue
            if wrote["lines"] is not None and place["line"] not in wrote["lines"]:
                continue
            key = "%s\u0000%s\u0000%s" % (finding.get("changeId") or finding.get("reason"), place["file"], place["line"])
            if key in state["notes"]:
                continue
            state["notes"].append(key)
            notes.append(_note_of(finding, place))
    return "Since.dev checked what was just written and found:\n%s" % "\n".join("- %s" % note for note in notes) if notes else None


# -- The finish check (Stop) ---------------------------------------------------------------------------------------------------------

STOP_TIMEOUT_MS = 10000
"""How long the finish check waits for Since: the agent has finished its turn, so a few seconds more costs nobody a step."""
MAX_FILES = 50
MANIFEST = re.compile(r"^(?:package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json|pom\.xml|build\.gradle(?:\.kts)?|[^/]+\.csproj)$", re.I)
LOCKFILE = re.compile(r"^(?:package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lock|poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|Gemfile\.lock"
                      r"|composer\.lock|packages\.lock\.json|gradle\.lockfile)$")
SKIPPED = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "target", ".next", "__pycache__"}


def _readable(path):
    """Whether a path inside the project is one the finish check reads: never in a dependency or build folder."""
    return not any(part == ".." or part in SKIPPED for part in path.split("/"))


def _nearest_declarations(io, cwd, path):
    """The nearest manifest at or above `path`'s folder and the lockfiles beside it, so imports are read at the versions the project installs."""
    parts = path.split("/")[:-1]
    for depth in range(len(parts), -1, -1):
        folder = "/".join(parts[:depth])
        names = sorted(entry["name"] for entry in io.list("%s/%s" % (cwd, folder) if folder else cwd) if not entry["dir"])
        manifests = [name for name in names if MANIFEST.search(name)]
        if manifests:
            return ["%s/%s" % (folder, name) if folder else name for name in manifests + [name for name in names if LOCKFILE.search(name)]]
    return []


def finish_files(io, cwd, written_paths, changed):
    """What the finish check sends: every file the session wrote or git shows changed that names something, then the manifests and
    lockfiles nearest each, as one set within the API's limits. `changed` are paths inside the project."""
    paths = [path for path in dict.fromkeys(list(written_paths) + list(changed)) if _readable(path)]
    out, seen, size = [], set(), 0

    def add(path, always):
        nonlocal size
        if path in seen or len(out) >= MAX_FILES:
            return
        seen.add(path)
        content = io.read_file("%s/%s" % (cwd, path))
        if content is None or not content.strip() or size + len(content) > MAX_CHARACTERS or (not always and not names_something(path, content)):
            return
        out.append({"path": path, "content": content})
        size += len(content)

    for path in paths:
        add(path, False)
    sent = [file["path"] for file in out]
    for path in sent:
        for declaration in _nearest_declarations(io, cwd, path):
            add(declaration, True)
    return out, sent


def stop_hook(ctx, event, state, check, changed, block=False):
    """The Stop hook: when the agent finishes its turn, the whole change is checked as one set of files, and the agent is told what is
    proven in the files it changed that it has not been told already, with what was covered. Nothing when the change is unchanged since
    the last finish check, clean, or Since is slow. `block` (the team's choice) makes the agent go on to fix it; otherwise the note waits
    for it. Returns the hook's JSON output, or None."""
    if not isinstance(event, dict) or event.get("stop_hook_active") is True:
        return None
    cwd = event.get("cwd").rstrip("/") if isinstance(event.get("cwd"), str) else None
    if not cwd:
        return None
    files, sent = finish_files(ctx["io"], cwd, list(state.get("written") or []) + list((state.get("files") or {}).keys()), changed)
    if not sent:
        return None
    seen = _hash(json.dumps([[file["path"], _hash(file["content"])] for file in files], separators=(",", ":"), ensure_ascii=False))
    if state.get("finished") == seen:
        return None
    answer = check({"files": files, "detail": "summary"})
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("findings"), list):
        return None
    state["finished"] = seen
    ours = set(sent)
    notes = []
    for finding in answer["json"]["findings"]:
        if not isinstance(finding, dict) or finding.get("state") != "affected":
            continue
        for place in finding.get("where") if isinstance(finding.get("where"), list) else []:
            if not isinstance(place, dict) or place.get("file") not in ours or not isinstance(place.get("line"), int):
                continue
            key = "%s\u0000%s\u0000%s" % (finding.get("changeId") or finding.get("reason"), place["file"], place["line"])
            if key in state["notes"]:
                continue
            state["notes"].append(key)
            notes.append(_note_of(finding, place))
    if not notes:
        return None
    one = len(sent) == 1
    covered = "%s changed in this session%s" % ("the file" if one else "the %d files" % len(sent),
                                                " with the manifests and lockfiles beside %s" % ("it" if one else "them") if len(files) > len(sent) else "")
    note = "Since.dev checked %s, and found:\n%s" % (covered, "\n".join("- %s" % line for line in notes))
    return {"decision": "block", "reason": note} if block else {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": note}}
