"""The write-time hook (Feature 5): Claude Code runs `since hook post-tool-use` after each file its agent writes. It checks the file
as it now stands, as one set of files, and tells the agent only what is proven on the lines it just wrote, in a short note. A file that
names no model, version, runtime or package is never sent, nor one unchanged since its last check, and the same note is never said
twice in a session. It answers within six seconds or stays silent: a slow or unreachable Since never holds the agent up longer."""

import hashlib
import json
import re

HOOK_TIMEOUT_MS = 6000
"""How long the hook waits for Since: past it, nothing is said and the agent goes on (the finish check says it later). A model id just
written is read closely by a model before Since answers: about 3 seconds in all in production (Nov 22, 2026), against 0.4 to 1.2 s for
any other file, so the wait is six seconds."""
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


def _written_id(line, name):
    """The quoted string on a line that is the model id the check names, or the alias or prefix it stands for; None when there is none."""
    wanted = str(name).lower()
    for match in re.finditer(r"[\"'`]([^\"'`\s]{3,})[\"'`]", line):
        value = match.group(1)
        lower = value.lower()
        if lower == wanted or wanted.startswith(lower + "-") or wanted.startswith(lower + "@") or wanted.endswith("/" + lower) or lower.endswith(wanted):
            return value
    return None


def _note_of(finding, place, content=None):
    """One note a line: the place, then what to do when the answer names a replacement (first, so it reads as the instruction it is,
    with a model id as the line writes it), then why in the check's own words, shortened, then the line to write and the source."""
    said = re.sub(r"^Names a model this change affects\.\s*", "", str(finding.get("reason") or "").strip())
    said = re.sub(r"\s*Moving to \S+ is more than a new id:[\s\S]*$", "", said)
    reason = re.sub(r"\.?$", ".", said, count=1)
    surface = finding.get("surface") if isinstance(finding.get("surface"), dict) else {}
    name = surface.get("name") or ""
    line_no = place.get("line") if isinstance(place.get("line"), int) and not isinstance(place.get("line"), bool) else None
    if surface.get("kind") == "model":
        lines = content.split("\n") if isinstance(content, str) else []
        written = _written_id(lines[line_no - 1], name) if line_no and 0 < line_no <= len(lines) else None
        subject = "%s here" % written if written else "the model id here (%s)" % name
    else:
        subject = " ".join(part for part in [name, finding.get("version")] if part)
    lead = "replace %s with %s. " % (subject, finding["replacement"]) if finding.get("replacement") and name else ""
    edit = finding.get("edit") if isinstance(finding.get("edit"), dict) else {}
    write = " Write: %s" % edit["to"].strip() if isinstance(edit.get("to"), str) and edit["to"].strip() else ""
    source = finding.get("source")
    url = source if isinstance(source, str) else source.get("url") if isinstance(source, dict) and isinstance(source.get("url"), str) else None
    return "%s%s: %s%s%s%s" % (place["file"], ":%s" % line_no if line_no else "", lead, reason, write, " Source: %s" % url if url else "")



def _note_text(head, lines):
    """The note as the agent reads it: what was checked, one line a finding, and the ask to fix them."""
    return "%s\n%s\nFix each one before you go on." % (head, "\n".join("- %s" % line for line in lines))


def post_tool_use(ctx, event, state, check):
    """The PostToolUse hook. `state` is the session's memory (each file's last checked text and the notes said). A patch that touches
    several files (Codex) is several writes, each checked as one, and one note says what they found. Returns the note for the agent, or
    None to say nothing."""
    notes = []
    for one in event if isinstance(event, list) else [event]:
        notes.extend(_write_notes(ctx, one, state, check))
    return _note_text("Since.dev checked what was just written and found:", notes) if notes else None


def _write_notes(ctx, event, state, check):
    """One write's notes: the findings proven on the lines it wrote that this session has not been told."""
    wrote = written(event, ctx["io"].read_file)
    if not wrote or not wrote["content"].strip() or len(wrote["content"]) > MAX_CHARACTERS or not names_something(wrote["path"], wrote["content"]):
        return []
    seen = _hash(wrote["content"])
    if state["files"].get(wrote["path"]) == seen:
        return []
    # Every file written that names something, answered or not: the finish check reads them all.
    state["written"] = list(dict.fromkeys((state.get("written") or []) + [wrote["path"]]))
    answer = check({"files": [{"path": wrote["path"], "content": wrote["content"]}], "detail": "summary"})
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("findings"), list):
        return []
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
            notes.append(_note_of(finding, place, wrote["content"]))
    return notes


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


def stop_hook(ctx, event, state, check, changed):
    """The Stop hook: when the agent finishes its turn, the whole change is checked as one set of files, and the agent is told what is
    proven in the files it changed that it has not been told already, with what was covered. Nothing when the change is unchanged since
    the last finish check, clean, or Since is slow. Returns the note, or None; the command words it as the agent takes it (clients.py)."""
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
            notes.append(_note_of(finding, place, next((file["content"] for file in files if file["path"] == place["file"]), None)))
    if not notes:
        return None
    one = len(sent) == 1
    covered = "%s changed in this session%s" % ("the file" if one else "the %d files" % len(sent),
                                                " with the manifests and lockfiles beside %s" % ("it" if one else "them") if len(files) > len(sent) else "")
    return _note_text("Since.dev checked %s, and found:" % covered, notes)


# -- The session brief (SessionStart) ------------------------------------------------------------------------------------------------

BRIEF_TIMEOUT_MS = 15000
"""How long the brief waits for Since: once a day a session starts a few seconds later; past it, the session starts with no brief."""
BRIEF_DAY_MS = 86400000
BRIEF_WALK_ENTRIES = 20000
BRIEF_MODEL_FILES = 20
BRIEF_FILE_CHARACTERS = 200000
RUNTIME_FILE = re.compile(r"^(?:\.nvmrc|\.node-version|\.python-version|\.tool-versions|global\.json|Dockerfile[^/]*|(?:docker-)?compose[^/]*\.ya?ml)$", re.I)
WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$", re.I)
CODE = re.compile(r"\.(?:[cm]?[jt]sx?|py|go|rb|java|kt|cs|php|rs|swift|scala|ex|exs|dart)$", re.I)
MODEL_ID = re.compile(r"[\"'`](?:gpt-|o[134](?:-|[\"'`])|chatgpt-|claude-|gemini-|text-embedding-|dall-e-|whisper-|mistral-|codestral-|command-r|deepseek-)", re.I)


def _depth_key(path):
    return (len(path.split("/")), path)


def brief_files(io, cwd):
    """What the brief sends, as one set within the API's limits: the manifests nearest the top first, the runtime and CI files, up to
    BRIEF_MODEL_FILES code files that name a model id, then the lockfiles beside the manifests while they fit."""
    found = {"manifests": [], "lockfiles": [], "runtimes": [], "code": []}
    seen = [0]

    def walk(folder):
        for entry in io.list("%s/%s" % (cwd, folder) if folder else cwd):
            seen[0] += 1
            if seen[0] > BRIEF_WALK_ENTRIES:
                return
            path = "%s/%s" % (folder, entry["name"]) if folder else entry["name"]
            if entry["dir"]:
                if entry["name"] not in SKIPPED and (not entry["name"].startswith(".") or entry["name"] == ".github" or path == ".github/workflows"):
                    walk(path)
            elif MANIFEST.search(entry["name"]):
                found["manifests"].append(path)
            elif LOCKFILE.search(entry["name"]):
                found["lockfiles"].append(path)
            elif RUNTIME_FILE.search(entry["name"]) or WORKFLOW.search(path):
                found["runtimes"].append(path)
            elif CODE.search(entry["name"]):
                found["code"].append(path)

    walk("")
    out = []
    size = [0]

    def add(path, test=lambda content: True):
        if len(out) >= MAX_FILES or any(file["path"] == path for file in out):
            return False
        content = io.read_file("%s/%s" % (cwd, path))
        limit = MAX_CHARACTERS if LOCKFILE.search(path.split("/")[-1]) else BRIEF_FILE_CHARACTERS
        if content is None or not content.strip() or len(content) > limit or size[0] + len(content) > MAX_CHARACTERS or not test(content):
            return False
        out.append({"path": path, "content": content})
        size[0] += len(content)
        return True

    for path in sorted(found["manifests"], key=_depth_key):
        add(path)
    for path in sorted(found["runtimes"], key=_depth_key):
        add(path)
    models = 0
    for path in sorted(found["code"], key=_depth_key):
        if models < BRIEF_MODEL_FILES and add(path, lambda content: MODEL_ID.search(content) is not None):
            models += 1
    folders = {"/".join(file["path"].split("/")[:-1]) for file in out if MANIFEST.search(file["path"].split("/")[-1])}
    for path in sorted(found["lockfiles"], key=_depth_key):
        if "/".join(path.split("/")[:-1]) in folders:
            add(path)
    return out


def _major(version):
    match = re.match(r"[^0-9]*([0-9]+)", str(version or ""))
    return int(match.group(1)) if match else None


def _place_of(where):
    for place in where if isinstance(where, list) else []:
        if isinstance(place, dict) and place.get("file"):
            return "%s:%s" % (place["file"], place["line"]) if isinstance(place.get("line"), int) else place["file"]
    return None


def _dated_line(finding):
    surface = finding.get("surface") if isinstance(finding.get("surface"), dict) else {}
    name = " ".join(part for part in [surface.get("name"), None if surface.get("kind") == "model" else finding.get("version")] if part)
    days = finding.get("daysLeft")
    if not isinstance(days, int) or isinstance(days, bool):
        when = ""
    elif days < 0:
        when = ", passed"
    elif days == 0:
        when = ", today"
    else:
        when = ", in %d %s" % (days, "day" if days == 1 else "days")
    if finding.get("effectiveDate"):
        date = "%s%s" % (finding["effectiveDate"], when)
    else:
        date = re.split(r"(?<=\.)\s", str(finding.get("reason") or "").strip())[0]
    where = _place_of(finding.get("where"))
    return "%s%s: %s%s" % (name, " (%s)" % where if where else "", date, "; use %s" % finding["replacement"] if finding.get("replacement") else "")


def brief_card(answer):
    """The brief's card from a check of files: runtimes and models with their dates, packages a major release behind, and the totals."""
    findings = answer.get("findings") if isinstance(answer.get("findings"), list) else []
    affected = [finding for finding in findings if isinstance(finding, dict) and finding.get("state") == "affected"]

    def of(kinds):
        return [finding for finding in affected if isinstance(finding.get("surface"), dict) and finding["surface"].get("kind") in kinds]

    runtimes = list(dict.fromkeys(_dated_line(finding) for finding in of(["runtime", "image"])))[:5]
    models = list(dict.fromkeys(_dated_line(finding) for finding in of(["model"])))[:5]
    checked = answer.get("checked") if isinstance(answer.get("checked"), list) else []
    behind = []
    for item in checked:
        if not isinstance(item, dict) or not isinstance(item.get("surface"), dict) or item["surface"].get("kind") != "package":
            continue
        have, latest = _major(item.get("version")), _major(item.get("latest"))
        if item.get("version") and item.get("latest") and have is not None and latest is not None and latest > have:
            behind.append((latest - have, len(behind), item))
    behind.sort(key=lambda entry: (-entry[0], entry[1]))
    behind = ["%s %s (latest %s)" % (item["surface"].get("name"), item["version"], item["latest"]) for _, _, item in behind[:5]]
    coverage = answer.get("coverage") if isinstance(answer.get("coverage"), dict) else {}
    as_of = coverage["asOf"][:10] if isinstance(coverage.get("asOf"), str) else None
    lines = ["Since.dev brief for this project%s (its manifests, runtime files and the files that name a model):" % (", as of %s" % as_of if as_of else "")]
    if runtimes:
        lines.append("- Runtimes and images: %s." % "; ".join(runtimes))
    if models:
        lines.append("- Models: %s." % "; ".join(models))
    if behind:
        lines.append("- A major release behind: %s." % ", ".join(behind))
    critical = len([finding for finding in affected if finding.get("severity") == "critical"])
    review = len([finding for finding in findings if isinstance(finding, dict) and finding.get("state") == "needs_review"])
    if affected:
        lines.append("- In all, %d %s this code%s%s. Check before you write against any of these." % (
            len(affected), "finding affects" if len(affected) == 1 else "findings affect", " (%d critical)" % critical if critical else "",
            ", and %d to review" % review if review else ""))
    else:
        lines.append("- Nothing it names is affected by a known change%s." % (" (%d to review)" % review if review else ""))
    return "\n".join(lines)


def session_start(ctx, event, cache, check):
    """The SessionStart hook: a short card of what the project runs on and what ended or is ending there, from one check of its files a
    day (sooner when those files changed); between checks the card is said again from the cache. Returns the card, or None. `cache` is
    the stored brief for this project, updated in place."""
    cwd = event.get("cwd").rstrip("/") if isinstance(event, dict) and isinstance(event.get("cwd"), str) else None
    if not cwd:
        return None
    files = brief_files(ctx["io"], cwd)
    if not files:
        return None
    seen = _hash(json.dumps([[file["path"], _hash(file["content"])] for file in files], separators=(",", ":"), ensure_ascii=False))
    now = ctx["io"].now()

    if cache.get("seen") == seen and isinstance(cache.get("card"), str) and now - (cache.get("at") or 0) < BRIEF_DAY_MS:
        return cache["card"]
    # The full answer: a summary counts what was checked, and the brief needs each package's version and latest.
    answer = check({"files": files, "detail": "full"})
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("findings"), list):
        return None
    card = brief_card(answer["json"])
    cache.update({"seen": seen, "at": now, "card": card})
    return card
