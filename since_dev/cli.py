"""The whole CLI: run(argv, io) returns the exit code. Everything it touches (network, files, clock, terminal) comes in
through io, so the tests replay it from fixtures, and src/cli.mjs prints the same text from the same fixtures.

Exit codes: 0 done. `check`: 1 when a finding fails, 2 when the caller has something to fix, 0 when Since could not be
reached (an outage never fails a build). Every other command: 1 when it could not finish, 2 for a usage error.
"""

import datetime
import hashlib
import json
import re
from urllib.parse import quote

from . import __version__ as VERSION
from . import render as text
from .init import dumps, init, loads
from .local import MAX_CHARACTERS, MAX_SET_FILES, check_body, file_sets, files_to_send, find_manifests, is_lockfile, parse_repo
from .hook import BRIEF_TIMEOUT_MS, HOOK_TIMEOUT_MS, STOP_TIMEOUT_MS, _nearest_declarations, names_something, post_tool_use, session_start, stop_hook

DEFAULT_API = "https://api.since.dev"
SITE = "https://since.dev"
APP = "https://app.since.dev"
COMMANDS = ["check", "calendar", "changed", "init", "login", "logout", "watches", "hook", "help", "version"]
SWITCHES = ["all", "json", "local", "watch", "staged", "dry-run", "yes", "hook", "pre-commit", "block", "no-color", "help", "version"]
OPTIONS = ["fail-on", "within", "provider", "ecosystem", "since", "to", "email", "max-files"]
FAIL_ON = ["low", "medium", "high", "critical", "none"]
MAX_PAGES = 10
MAX_WAIT_MS = 60000


def parse_args(argv):
    args = {"command": None, "positional": [], "flags": {}, "error": None}
    index = 0
    while index < len(argv):
        word = argv[index]
        index += 1
        if word == "-h":
            args["flags"]["help"] = True
        elif word == "-v":
            args["flags"]["version"] = True
        elif word.startswith("--"):
            equals = word.find("=")
            name = word[2:] if equals < 0 else word[2:equals]
            if name in SWITCHES and equals < 0:
                args["flags"][name] = True
            elif name in OPTIONS:
                if equals >= 0:
                    value = word[equals + 1:]
                elif index < len(argv):
                    value = argv[index]
                    index += 1
                else:
                    value = None
                if value is None or value == "":
                    args["error"] = "--%s needs a value." % name
                    return args
                args["flags"][name] = value
            else:
                args["error"] = "Unknown option: %s" % word
                return args
        elif args["command"] is None and not args["positional"] and word in COMMANDS:
            args["command"] = word
        else:
            args["positional"].append(word)
    return args


def help_text(p):
    usage = [
        ["", "check this repository (reads the origin remote)"],
        [" <owner/repo>", "check a public GitHub repository"],
        [" check", "the same for CI: exit 1 when a finding fails"],
        [" calendar", "what ends in the next 30 days"],
        [" changed <package>", "breaking changes and deprecations of a package"],
        [" init", "set up your coding agent to check before it writes"],
        [" login", "sign in, for private repositories"],
        [" logout", "remove the stored key"],
        [" watches", "list what this workspace watches"],
        [" watches remove <id>", "stop watching one"],
        [" hook post-tool-use", "run by your coding agent after each write: what it wrote, checked"],
        [" hook stop", "run by your coding agent when it finishes: the whole change, checked"],
        [" hook session-start", "run by your coding agent as a session starts: this project's brief"],
    ]
    options = [
        ["--all", "show every finding"],
        ["--json", "print the answer as JSON"],
        ["--local", "check the manifests in this folder, not GitHub"],
        ["--watch", "check: keep watching the manifests here"],
        ["--fail-on <level>", "check: low, medium, high (default), critical or none"],
        ["--within <days>", "only dates within this many days, as 30d"],
        ["--provider <name>", "calendar: one provider, as openai"],
        ["--ecosystem <name>", "changed: the registry, as npm (default) or pypi"],
        ["--since <version>", "changed: only what changed after this version"],
        ["--to <version>", "changed: up to this version, with how each release was read"],
        ["--email <address>", "login: the email to sign in with"],
        ["--dry-run", "init: show what would be written"],
        ["--yes", "init: write without asking"],
        ["--hook", "init: also add the write-time hooks for Claude Code, for you only"],
        ["--block", "hook stop: the agent fixes what is found before it stops"],
        ["--pre-commit", "init: also add a git pre-commit hook that runs check --staged"],
        ["--staged", "check: only what git has staged, for a pre-commit hook"],
        ["--max-files <n>", "how many manifests and lockfiles to send (default 50)"],
        ["--no-color", "plain text"],
    ]
    width = max(len(p) + len(words) for words, _ in usage) + 3
    lines = ["since-dev %s: what in this repository is about to stop working" % VERSION, "", "Usage"]
    for words, what in usage:
        lines.append("  %s%s" % ((p + words).ljust(width), what))
    lines.extend(["", "Options"])
    for words, what in options:
        lines.append("  %s%s" % (words.ljust(21), what))
    lines.extend(["", "Docs  %s/docs#cli" % SITE, ""])
    return "\n".join(lines)


def clamp(value, low, high):
    return min(high, max(low, value))


def whole_number(value):
    return int(value) if text.is_number(value) else None


def first(*values):
    """The first value that is not None, as ?? reads in the JavaScript."""
    for value in values:
        if value is not None:
            return value
    return None


def read_json(content):
    if content is None:
        return None
    try:
        data = loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def credentials_path(io):
    if io.platform == "win32" and io.env.get("APPDATA"):
        return "%s\\since\\credentials.json" % io.env["APPDATA"]
    return "%s/since/credentials.json" % (io.env.get("XDG_CONFIG_HOME") or "%s/.config" % io.home)


def read_key(io, api):
    """The key to call with: SINCE_DEV_KEY, or the one `login` stored. A stored key only ever goes to the API it came from."""
    from_env = str(io.env.get("SINCE_DEV_KEY") or "").strip()
    if from_env:
        return {"value": from_env, "source": "env"}
    stored = read_json(io.read_file(credentials_path(io)))
    if stored and isinstance(stored.get("api_key"), str) and stored["api_key"] and (stored.get("api") or DEFAULT_API) == api:
        return {"value": stored["api_key"], "source": "file"}
    return None


def encode_query(query):
    # The characters encodeURIComponent leaves alone, so both languages ask for the same address.
    return "&".join(
        "%s=%s" % (name, quote(str(value), safe="-_.!~*'()"))
        for name, value in query.items()
        if value is not None and value != ""
    )


def call(ctx, method, path, query=None, body=None, key=None, timeout_ms=None):
    """One request. status 0 means Since could not be reached; json is the answer when it is a JSON object."""
    search = encode_query(query) if query else ""
    headers = {"User-Agent": "since-cli/%s (%s)" % (VERSION, ctx["io"].runtime), "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer %s" % key["value"]
    try:
        url = "%s%s%s" % (ctx["api"], path, "?" + search if search else "")
        payload = None if body is None else json.dumps(body)
        answer = ctx["io"].fetch(method, url, headers, payload, timeout_ms) if timeout_ms else ctx["io"].fetch(method, url, headers, payload)
    except Exception as error:  # noqa: BLE001 - any failure to reach Since reads the same to the caller
        return {"status": 0, "json": None, "retryAfter": None, "certificate": "CERTIFICATE_VERIFY_FAILED" in str(error)}
    retry = re.match(r"\s*([+-]?[0-9]+)", str(first(answer.get("retryAfter"), "")))
    return {"status": answer["status"], "json": read_json(answer.get("text")), "retryAfter": int(retry.group(1)) if retry else None}


def error_of(answer):
    return answer["json"]["error"] if answer["json"] is not None and isinstance(answer["json"].get("error"), str) else None


def message_of(answer):
    return answer["json"]["message"] if answer["json"] is not None and isinstance(answer["json"].get("message"), str) else None


def refuse(ctx, answer):
    """Say why a request did not answer, and return the exit code that goes with it."""
    io, p, check = ctx["io"], ctx["p"], ctx["check"]
    message = message_of(answer)
    if answer["status"] == 0 or answer["status"] >= 500:
        what = "Since is still busy." if error_of(answer) == "busy" else "Since could not be reached."
        if answer.get("certificate"):
            what = "This Python could not verify Since's certificate (on macOS, run its Install Certificates command)."
        io.err("%s Try again in a minute.%s\n" % (what, " Nothing was checked, so this run passes." if check else ""))
        return 0 if check else 1
    if answer["status"] == 401:
        if ctx["key"] and ctx["key"]["source"] == "env":
            io.err("Since refused the key in SINCE_DEV_KEY. Create a new one at %s/api-keys\n" % APP)
        else:
            io.err("Since refused the stored key. Sign in again: %s login\n" % p)
    elif answer["status"] == 429:
        seconds = first(whole_number(answer["json"].get("retryAfterSec") if answer["json"] is not None else None), answer["retryAfter"])
        # A shared CI address runs out of free scans through no fault of the build, so a limit passes a check like an outage does.
        io.err("%s%s%s\n" % (
            first(message, "Too many requests."),
            " Try again in %s." % text.wait(seconds) if seconds else "",
            " Nothing was checked, so this run passes." if check else "",
        ))
        return 0 if check else 1
    else:
        io.err("%s\n" % first(message, "Since answered %d." % answer["status"]))
    return 2 if check else 1


def public_scan(ctx, full_name):
    """The free scan of a public GitHub repository. A busy answer is waited out up to three times.

    A connection that drops is asked again twice: a long scan can outlast a connection, and asking again costs nothing,
    since a scan still running is joined and a finished one is served.
    """
    waits = 0
    drops = 0
    while True:
        answer = call(ctx, "POST", "/v1/scan", body={"repo": full_name})
        if answer["status"] == 200 and answer["json"] is not None and isinstance(answer["json"].get("repository"), dict):
            return {"result": answer["json"]}
        if answer["status"] == 503 and error_of(answer) == "busy" and waits < 3:
            seconds = clamp(first(answer["retryAfter"], 20), 1, 60)
            ctx["io"].err("Since is busy. Trying again in %s.\n" % text.wait(seconds))
            ctx["io"].sleep(seconds * 1000)
            waits += 1
            continue
        if answer["status"] == 0 and drops < 2:
            drops += 1
            ctx["io"].err("The connection dropped before the answer came. Asking again.\n")
            continue
        return {"answer": answer}


def _has_findings(answer):
    return answer["status"] == 200 and answer["json"] is not None and isinstance(answer["json"].get("findings"), list)


def check_all(ctx, body):
    """One keyed check, whole: waits while a connected repository is still being read, then follows every page (pages are free)."""
    io = ctx["io"]
    started = io.now()
    answer = call(ctx, "POST", "/v1/check", body=body, key=ctx["key"])
    while _has_findings(answer) and answer["json"].get("scanning") and not answer["json"]["findings"]:
        scanning = answer["json"]["scanning"]
        seconds = clamp(first(whole_number(scanning.get("retryAfterSec") if isinstance(scanning, dict) else None), 10), 1, 30)
        if io.now() - started + seconds * 1000 > MAX_WAIT_MS:
            break
        io.err("Since is still reading this repository. Trying again in %s.\n" % text.wait(seconds))
        io.sleep(seconds * 1000)
        answer = call(ctx, "POST", "/v1/check", body=body, key=ctx["key"])
    if not _has_findings(answer):
        return answer
    whole = answer["json"]
    page = 1
    while page < MAX_PAGES and isinstance(whole.get("nextCursor"), str) and whole["nextCursor"]:
        following = call(ctx, "POST", "/v1/check", body=dict(body, cursor=whole["nextCursor"]), key=ctx["key"])
        if not _has_findings(following):
            break
        whole["findings"] = whole["findings"] + following["json"]["findings"]
        whole["nextCursor"] = following["json"].get("nextCursor")
        page += 1
    if whole.get("nextCursor"):
        whole["truncated"] = True
    return answer


def connected_check(ctx, full_name):
    """A repository connected to the caller's workspace, checked where Since already has its code."""
    repos = call(ctx, "GET", "/v1/repos", key=ctx["key"])
    if repos["status"] != 200 or repos["json"] is None:
        return {"exit": refuse(ctx, repos)}
    listed = repos["json"].get("repos")
    repo = None
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict) and str(entry.get("fullName")).lower() == full_name.lower():
            repo = entry
            break
    if repo is None:
        return {}
    answer = check_all(ctx, {"repo": repo.get("id"), "detail": "summary"})
    if answer["status"] == 403 and error_of(answer) == "impact_api_not_in_plan":
        return {}
    if answer["status"] != 200 or answer["json"] is None:
        return {"exit": refuse(ctx, answer)}
    return {"answer": answer["json"], "repo": repo}


# The longest watch name the API takes, in UTF-16 code units as it counts them.
WATCH_NAME_MAX = 100


def watch_name(io):
    """What a watch of this folder is called: owner/repo from the origin remote, else the folder's own name. Always a name the API takes:
    on one line, at most 100 characters (cut at a whole character), and never a bare true or false, which the API reads as turning
    watching on or off: a folder named false is watched as ./false."""
    repo = parse_repo(io.origin())
    if repo:
        raw = "%s/%s" % (repo["owner"], repo["name"])
    else:
        parts = [part for part in re.split(r"[\\/]", io.cwd) if part]
        raw = parts[-1] if parts else ""
    name, units = "", 0
    for character in re.sub(r"[\x00-\x1f\x7f]+", " ", raw).strip():
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > WATCH_NAME_MAX:
            break
        name, units = name + character, units + width
    name = name.strip() or "this folder"
    return "./" + name if re.fullmatch(r"(?:true|false)", name, re.IGNORECASE) else name


def local_check(ctx, no_origin):
    """The manifests in this folder, each with the lockfiles beside it, sent together as sets of files, so the versions read are the
    installed ones and a range with no lockfile is read as a fresh install would land. With --watch, each manifest is sent and watched on
    its own, under the folder's name: a watch keeps one file."""
    io, p, check = ctx["io"], ctx["p"], ctx["check"]
    if not ctx["key"]:
        if no_origin:
            io.err("This folder has no GitHub origin. Name a public repository, as %s owner/repo, or sign in to check the files here: %s login\n" % (p, p))
        else:
            io.err("Checking the files in this folder needs a key. Sign in first: %s login\n" % p)
        return 2 if check else 1
    found = find_manifests(io)
    if not found:
        io.err("No manifests in this folder (package.json, pyproject.toml, go.mod and the like).\n")
        return 2 if check else 1
    if not ctx["args"]["flags"].get("watch"):
        return local_sets(ctx, found)
    files = []
    notes = []
    watch = watch_name(io)
    last = None
    if len(found) > ctx["max_files"]:
        notes.append("%s found, the first %d checked (each one costs credits). Raise it with --max-files." % (text.plural(len(found), "manifest"), ctx["max_files"]))
    for file in found[:ctx["max_files"]]:
        content = io.read_file("%s/%s" % (io.cwd, file))
        if content is None:
            notes.append("%s: could not be read." % file)
        elif len(content) > MAX_CHARACTERS:
            notes.append("%s: over %s characters, not sent." % (file, text.num(MAX_CHARACTERS)))
        else:
            body = dict(check_body(file, content), watch=watch)
            answer = check_all(ctx, body)
            if _has_findings(answer):
                files.append({"path": file, "answer": answer["json"]})
            elif answer["status"] in (401, 402):
                return refuse(ctx, answer)
            else:
                last = answer
                unreachable = answer["status"] == 0 or answer["status"] >= 500
                notes.append("%s: %s" % (file, "Since could not be reached." if unreachable else first(message_of(answer), "Since answered %d." % answer["status"])))
    if not files and last is not None:
        return refuse(ctx, last)
    return report(ctx, {"kind": "local", "files": files, "notes": notes})


def staged_check(ctx):
    """`since check --staged`, for a git pre-commit hook: the staged files that name a model, a version, a runtime or a package, as git has
    staged them, with the manifests and lockfiles nearest them, checked as one set. Only findings in the staged files count, so a commit
    is never held for what it did not touch."""
    io, p, check = ctx["io"], ctx["p"], ctx["check"]
    if not ctx["key"]:
        io.err("Checking staged files needs a key. Sign in first: %s login\n" % p)
        return 2 if check else 1
    root = (io.root() or io.cwd).rstrip("/")
    staged = [path for path in (io.staged() if hasattr(io, "staged") else []) if not any(part in ("node_modules", ".git") for part in path.split("/"))]

    def read(path):
        return io.staged_file(path) if path in staged else io.read_file("%s/%s" % (root, path))

    named = []
    for path in staged:
        content = read(path)
        if content is not None and content.strip() and len(content) <= MAX_CHARACTERS and names_something(path, content):
            named.append({"path": path, "content": content})
    if not named:
        io.out("Nothing staged names a model, a version, a runtime or a package.\n")
        return 0
    files = []
    size = [0]

    def add(file):
        if len(files) >= MAX_SET_FILES or any(other["path"] == file["path"] for other in files) or size[0] + len(file["content"]) > MAX_CHARACTERS:
            return
        files.append(file)
        size[0] += len(file["content"])

    for file in named:
        add(file)
    notes = ["%s name something; the first %d were checked." % (text.plural(len(named), "staged file"), len(files))] if len(named) > len(files) else []
    for file in named:
        for path in _nearest_declarations(io, root, file["path"]):
            content = read(path)
            if content is not None and content.strip():
                add({"path": path, "content": content})
    answer = check_all(ctx, {"files": files, "detail": "summary"})
    if not _has_findings(answer):
        return refuse(ctx, answer)
    mine = {file["path"] for file in named}
    findings = [finding for finding in answer["json"]["findings"]
                if any(isinstance(place, dict) and place.get("file") in mine for place in (finding.get("where") if isinstance(finding.get("where"), list) else []))]
    kept = dict(answer["json"], findings=findings)
    return report(ctx, {"kind": "local", "staged": True, "files": [{"path": None, "answer": kept}], "drafts": [{"files": [file["path"] for file in files], "answer": kept}], "notes": notes})


def local_sets(ctx, found):
    """The folder's manifests and the lockfiles beside them, in sets of files."""
    io = ctx["io"]
    listed = files_to_send(io, found)
    notes = []
    if len(listed) > ctx["max_files"]:
        notes.append("%s found, the first %d sent. Raise it with --max-files." % (text.plural(len(listed), "file"), ctx["max_files"]))
    entries = []
    for path in listed[:ctx["max_files"]]:
        content = io.read_file("%s/%s" % (io.cwd, path))
        if content is None:
            notes.append("%s: could not be read." % path)
        elif len(content) > MAX_CHARACTERS:
            notes.append("%s: over %s characters, not sent%s." % (path, text.num(MAX_CHARACTERS), "; its manifest was read as a fresh install would land" if is_lockfile(path) else ""))
        else:
            entries.append({"path": path, "content": content})
    drafts = []
    last = None
    for group in file_sets(entries):
        answer = check_all(ctx, {"files": group, "detail": "summary"})
        paths = [file["path"] for file in group]
        if _has_findings(answer):
            drafts.append({"files": paths, "answer": answer["json"]})
        elif answer["status"] in (401, 402):
            return refuse(ctx, answer)
        else:
            last = answer
            unreachable = answer["status"] == 0 or answer["status"] >= 500
            notes.append("%s: %s" % (", ".join(paths), "Since could not be reached." if unreachable else first(message_of(answer), "Since answered %d." % answer["status"])))
    if not drafts and last is not None:
        return refuse(ctx, last)
    return report(ctx, {"kind": "local", "files": [{"path": None, "answer": draft["answer"]} for draft in drafts], "drafts": drafts, "notes": notes})


def hook_state_path(io, session):
    """A hook event's session file: what this session's hook checked and said."""
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(session if session is not None else ""))[:80] or "session"
    return "%s/.cache/since-dev/hooks/%s.json" % (io.home, name)


def brief_path(io, cwd):
    """Where a project's brief is kept between sessions: by its folder, never its contents."""
    return "%s/.cache/since-dev/briefs/%s.json" % (io.home, hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:32])


def brief_command(ctx, event):
    """`since hook session-start`: the project's brief, said at the start of each session, checked again once a day or when its files
    change."""
    io = ctx["io"]
    file = brief_path(io, event.get("cwd"))
    cache = {}
    try:
        saved = json.loads(io.read_file(file) or "null")
        if isinstance(saved, dict):
            cache = saved
    except ValueError:
        pass
    before = json.dumps(cache, separators=(",", ":"), ensure_ascii=False)
    output = session_start(ctx, event, cache, lambda body: call(ctx, "POST", "/v1/check", body=body, key=ctx["key"], timeout_ms=BRIEF_TIMEOUT_MS))
    if json.dumps(cache, separators=(",", ":"), ensure_ascii=False) != before:
        try:
            io.write_file(file, json.dumps(cache, separators=(",", ":"), ensure_ascii=False), 0o600)
        except OSError:
            pass
    if output:
        io.out("%s\n" % json.dumps(output, separators=(",", ":"), ensure_ascii=False))
    return 0


def changed_paths(io, cwd):
    """The files git shows changed or new under the project folder, as paths inside it."""
    if not isinstance(cwd, str) or not hasattr(io, "changed"):
        return []
    base = cwd.rstrip("/")
    root = (io.root() or base).rstrip("/")
    out = []
    for path in io.changed() or []:
        full = "%s/%s" % (root, path)
        if full.startswith(base + "/"):
            out.append(full[len(base) + 1:])
    return out


def hook_command(ctx, name):
    """`since hook <event>`, run by a coding agent's hooks (Claude Code). It reads the event from stdin and always exits 0: a check that
    cannot answer in time says nothing, so the hook never stops the agent."""
    io = ctx["io"]
    if name not in ("post-tool-use", "stop", "session-start"):
        io.err("%s hook takes post-tool-use, stop or session-start.\n" % ctx["p"])
        return 0
    try:
        event = json.loads(io.stdin())
    except ValueError:
        return 0
    if not ctx["key"] or not isinstance(event, dict):
        return 0
    if name == "session-start":
        return brief_command(ctx, event)
    file = hook_state_path(io, event.get("session_id"))
    state = {"files": {}, "notes": []}
    try:
        saved = json.loads(io.read_file(file) or "null")
        if isinstance(saved, dict) and isinstance(saved.get("files"), dict) and isinstance(saved.get("notes"), list):
            state = saved
    except ValueError:
        pass
    output = None
    if name == "stop":
        changed = changed_paths(io, event.get("cwd"))
        output = stop_hook(ctx, event, state, lambda body: call(ctx, "POST", "/v1/check", body=body, key=ctx["key"], timeout_ms=STOP_TIMEOUT_MS), changed,
                           block=bool(ctx["args"]["flags"].get("block")))
    else:
        note = post_tool_use(ctx, event, state, lambda body: call(ctx, "POST", "/v1/check", body=body, key=ctx["key"], timeout_ms=HOOK_TIMEOUT_MS))
        if note:
            output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
    try:
        io.write_file(file, json.dumps(state, separators=(",", ":"), ensure_ascii=False), 0o600)
    except OSError:
        pass
    if output:
        io.out("%s\n" % json.dumps(output, separators=(",", ":"), ensure_ascii=False))
    return 0


def answer_as_of(answers):
    """The oldest `coverage.asOf` of the answers: what they are all current as of."""
    known = sorted(answer["coverage"]["asOf"] for answer in answers if isinstance(answer, dict) and isinstance(answer.get("coverage"), dict)
                   and isinstance(answer["coverage"].get("asOf"), str) and answer["coverage"]["asOf"])
    return known[0] if known else None


def current_as_of(io, at):
    """"current as of 22 min ago": how far the sources an answer read had been read, when it says."""
    parsed = parse_time(at if isinstance(at, str) else "")
    ago = text.ago(None if parsed is None else io.now() - parsed)
    return "current as of %s" % ago if ago else ""


def parse_time(value):
    """Milliseconds since 1970 from an ISO time, or None."""
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return (moment - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)) // datetime.timedelta(milliseconds=1)


def report(ctx, data):
    """Print what was found and return the exit code."""
    io, args, p, c, check = ctx["io"], ctx["args"], ctx["p"], ctx["c"], ctx["check"]
    show_all = bool(args["flags"].get("all"))
    if data["kind"] == "scan":
        findings = data["result"].get("findings")
        items = text.scan_items(findings if isinstance(findings, list) else [])
    elif data["kind"] == "repo":
        items = text.check_items(data["answer"]["findings"], None, p)
    else:
        items = [item for file in data["files"] for item in text.check_items(file["answer"]["findings"], file["path"], p)]
    if ctx["within"] is not None:
        items = [item for item in items if item["daysLeft"] is not None and item["daysLeft"] <= ctx["within"]]
    items = text.order(items)
    failing = [item for item in items if text.fails(item, ctx["fail_on"])] if check else []
    exit_code = 1 if failing else 0
    if args["flags"].get("json"):
        whole = data["result"] if data["kind"] == "scan" else data["answer"] if data["kind"] == "repo" else {"drafts": data["drafts"]} if data.get("drafts") is not None else {"files": data["files"]}
        io.out("%s\n" % dumps(whole, 2))
        return exit_code

    lines = [""]
    notes = []
    footer = []
    counts = text.counts_line(items, ctx["within"])
    more = "%s%s%s --all" % (p, " check" if check else "", " %s" % args["positional"][0] if args["positional"] else "")
    if data["kind"] == "scan":
        result = data["result"]
        repository = result["repository"]
        summary = result["summary"] if isinstance(result.get("summary"), dict) else {}
        sha = str(first(repository.get("headSha"), ""))[:7]
        branch = str(first(repository.get("defaultBranch"), ""))
        parsed = parse_time(first(result.get("scannedAt"), ""))
        scanned = text.ago(None if parsed is None else io.now() - parsed)
        head = [c.bold(str(repository.get("fullName"))), "%s@%s" % (branch, sha) if sha else branch, "scanned %s" % scanned if scanned else "", current_as_of(io, result.get("asOf"))]
        lines.append("  %s" % "  ".join(part for part in head if part))
        dependencies = first(whole_number(summary.get("dependencies")), 0)
        files = first(whole_number(summary.get("files")), 0)
        lines.append("  %s   %s %s and %s read" % (counts, text.num(dependencies), "dependency" if dependencies == 1 else "dependencies", text.plural(files, "file")))
        if data["fromTree"]:
            lines.append(c.dim("  Read from GitHub (%s), not your working tree." % branch))
        coverage = result.get("coverage")
        for note in coverage["notes"] if isinstance(coverage, dict) and isinstance(coverage.get("notes"), list) else []:
            notes.append(str(note))
        footer.append("  %s%s/scan/%s?utm_source=cli" % ("Full report".ljust(14), SITE, repository.get("fullName")))
    elif data["kind"] == "repo":
        head = [c.bold(str(data["repo"].get("fullName"))), str(first(data["repo"].get("defaultBranch"), "")), "connected repository", current_as_of(io, answer_as_of([data["answer"]]))]
        lines.append("  %s" % "  ".join(part for part in head if part))
        lines.append("  %s   %s checked" % (counts, text.num(first(whole_number(data["answer"].get("checkedCount")), 0))))
        notes.extend(text.answer_notes([data["answer"]]))
    else:
        checked = sum(first(whole_number(file["answer"].get("checkedCount")), 0) for file in data["files"])
        # Sets of files count the files sent; watched manifests, the manifests.
        sent = text.plural(sum(len(draft["files"]) for draft in data["drafts"]), "file") if data.get("drafts") is not None else text.plural(len(data["files"]), "manifest")
        head = [c.bold("Staged" if data.get("staged") else "This folder"), "%s checked" % sent, current_as_of(io, answer_as_of([file["answer"] for file in data["files"]]))]
        lines.append("  %s" % "  ".join(part for part in head if part))
        lines.append("  %s   %s checked" % (counts, text.num(checked)))
        notes.extend(data["notes"])
        # A file --watch could not watch says why, as the answer words it.
        for file in data["files"]:
            watch = file["answer"].get("watch")
            if isinstance(watch, dict) and watch.get("status") != "watching":
                notes.append("%s: %s" % (file["path"], watch.get("message")))
        notes.extend(text.answer_notes([file["answer"] for file in data["files"]]))
        # A staged check reads the code it stages: the note is for the manifests-only checks.
        if not data.get("staged"):
            notes.append("Only %s were checked. Connect the repository to check its code too: %s?utm_source=cli" % ("manifests and lockfiles" if data.get("drafts") is not None else "manifests", APP))
    lines.append("")
    lines.extend(text.finding_lines(items, show_all, c, more))
    if data["kind"] == "local":
        lines.extend(watching_lines(ctx, data["files"]))
    for note in notes:
        lines.append("  %s" % note)
    if notes:
        lines.append("")
    lines.extend(footer)
    if not check:
        lines.append("  %s%s init   your coding agent checks before it writes" % ("Next".ljust(14), p))
    elif ctx["fail_on"] != "none":
        level = "at critical" if ctx["fail_on"] == "critical" else "at %s or above" % ctx["fail_on"]
        if failing:
            lines.append("  %s%s %s" % ("Failed".ljust(14), text.plural(len(failing), "finding"), level))
        else:
            lines.append("  %snothing %s" % ("Passed".ljust(14), level))
    lines.append("")
    io.out("\n".join(lines))
    return exit_code


def watching_lines(ctx, files):
    """What --watch made of the manifests: how many are watched, until when and where new findings go. Nothing when none is watched."""
    watches = [file["answer"]["watch"] for file in files if isinstance(file["answer"].get("watch"), dict) and file["answer"]["watch"].get("status") == "watching"]
    if not watches:
        return []
    dates = sorted(str(first(watch.get("expiresAt"), ""))[:10] for watch in watches if str(first(watch.get("expiresAt"), ""))[:10])
    until = dates[0] if dates else None
    name = next((watch["name"] for watch in watches if isinstance(watch.get("name"), str) and watch["name"]), None)
    webhooks = max([0] + [first(whole_number(watch["delivery"].get("webhooks")), 0) for watch in watches if isinstance(watch.get("delivery"), dict)])
    email = any(isinstance(watch.get("delivery"), dict) and watch["delivery"].get("email") is True for watch in watches)
    if webhooks:
        where = "New findings go to %s%s. List them: %s watches" % (text.plural(webhooks, "webhook endpoint"), " and by email" if email else "", ctx["p"])
    elif email:
        where = "New findings go by email. List them: %s watches" % ctx["p"]
    else:
        where = "Nothing tells you about new findings yet. Add a webhook: %s/api-keys#webhooks" % APP
    head = "%s%s%s" % (text.plural(len(watches), "manifest"), " of %s" % name if name else "", " until %s" % until if until else "")
    return ["  %s%s" % ("Watching".ljust(14), head), "%s%s" % (" " * 16, where), ""]


def repo_command(ctx):
    """`since`, `since <owner/repo>` and `since check`: find the repository, then the cheapest way to read it."""
    io, args, p, check = ctx["io"], ctx["args"], ctx["p"], ctx["check"]
    given = args["positional"][0] if args["positional"] else None
    from_tree = given is None
    repo = parse_repo(io.origin() if from_tree else given)
    stop = 2 if check else 1
    if not from_tree and not repo:
        io.err("%s is not a repository. Use owner/repo or a GitHub URL.\n" % given)
        return 2
    if not from_tree and args["flags"].get("local"):
        io.err("--local checks the folder you are in, so it takes no repository name.\n")
        return 2
    if args["flags"].get("staged"):
        if not from_tree or args["flags"].get("watch"):
            io.err("--staged checks what git has staged in the folder you are in, so it takes %s.\n" % ("no --watch" if args["flags"].get("watch") else "no repository name"))
            return 2
        return staged_check(ctx)
    # A watch keeps what the manifests here hold, read from this folder: a connected repository is watched already, by connecting it.
    if args["flags"].get("watch"):
        if not from_tree:
            io.err("--watch watches the manifests in the folder you are in, so it takes no repository name.\n")
            return 2
        if not ctx["key"]:
            io.err("Watching needs a key. Sign in first: %s login\n" % p)
            return 2 if check else 1
        return local_check(ctx, False)
    # In CI with a key, and whenever --local asks: the checkout itself, not the default branch on GitHub.
    if from_tree and (args["flags"].get("local") or (check and ctx["key"])):
        return local_check(ctx, False)
    if not repo or repo["host"] != "github.com":
        if from_tree:
            return local_check(ctx, True)
        io.err("%s is not on GitHub. Run %s inside its folder to check its manifests.\n" % (given, p))
        return stop

    full_name = "%s/%s" % (repo["owner"], repo["name"])
    # A first scan takes up to a minute, and a silent terminal reads as a hung one.
    # Run inside a repository, the name comes from its origin remote: say so, or the name looks like part of the command.
    io.err("Reading %s on GitHub. A first scan can take a minute.\n" % ("this folder's repository (%s)" % full_name if from_tree else full_name))
    scan = public_scan(ctx, full_name)
    if "result" in scan:
        return report(ctx, {"kind": "scan", "result": scan["result"], "fromTree": from_tree})
    # The free scan said no (not public, too big, or today's scans are used up): with a key there is still a way to answer
    # for the folder you are in, so say why and take it. Without one, or when Since itself is down, this is the end.
    refusal = error_of(scan["answer"])
    down = scan["answer"]["status"] == 0 or scan["answer"]["status"] >= 500
    if refusal != "not_found":
        if down or not ctx["key"] or not from_tree:
            return refuse(ctx, scan["answer"])
        why = message_of(scan["answer"])
        io.err("%sChecking with your key instead.\n" % ("%s " % why if why is not None else ""))
    if not ctx["key"]:
        if from_tree:
            io.err("%s is not a public GitHub repository. For a private one, sign in and run this again: %s login\n" % (full_name, p))
        else:
            io.err("GitHub serves no public repository named %s. For a private one, sign in first: %s login\n" % (full_name, p))
        return stop
    connected = connected_check(ctx, full_name)
    if "answer" in connected:
        return report(ctx, {"kind": "repo", "answer": connected["answer"], "repo": connected["repo"]})
    if "exit" in connected:
        return connected["exit"]
    if from_tree:
        return local_check(ctx, False)
    io.err("%s is private and Since cannot read it from here. Run %s inside its folder.\n" % (full_name, p))
    return stop


def calendar_command(ctx):
    io, args, p, c = ctx["io"], ctx["args"], ctx["p"], ctx["c"]
    within = first(ctx["within"], 30)
    provider = args["flags"].get("provider")
    answer = call(ctx, "GET", "/v1/calendar", query={"within": "%dd" % within, "provider": provider, "limit": 500 if args["flags"].get("all") else 15})
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("entries"), list):
        return refuse(ctx, answer)
    if args["flags"].get("json"):
        io.out("%s\n" % dumps(answer["json"], 2))
        return 0
    entries = answer["json"]["entries"]
    lines = ["", "  %s%s" % (c.bold("Ending in the next %s" % text.plural(within, "day")), "  %s" % provider if provider else ""), ""]
    if not entries:
        lines.append("  Nothing on the calendar in that time.")
    lines.extend(text.calendar_lines(entries, SITE, c))
    lines.append("")
    if answer["json"].get("nextCursor"):
        lines.append("  %s%s calendar%s --all" % ("More".ljust(14), p, " --provider %s" % provider if provider else ""))
    lines.extend(["  %s%s/calendar?utm_source=cli" % ("Calendar".ljust(14), SITE), ""])
    io.out("\n".join(lines))
    return 0


def changed_command(ctx):
    io, args, p, c = ctx["io"], ctx["args"], ctx["p"], ctx["c"]
    name = args["positional"][0]
    if not ctx["key"]:
        io.err("%s changed needs a key. Sign in first: %s login\n" % (p, p))
        return 1
    ecosystem = first(args["flags"].get("ecosystem"), "npm")
    since = args["flags"].get("since")
    to = args["flags"].get("to")
    show_all = bool(args["flags"].get("all"))
    answer = call(ctx, "GET", "/v1/changes", query={"package": name, "ecosystem": ecosystem, "since": since, "to": to, "limit": 200 if show_all else 20}, key=ctx["key"])
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("changes"), list):
        return refuse(ctx, answer)
    if args["flags"].get("json"):
        io.out("%s\n" % dumps(answer["json"], 2))
        return 0
    changes = text.unique_changes(answer["json"]["changes"])
    found = answer["json"]["package"] if isinstance(answer["json"].get("package"), dict) else {}
    head = [c.bold(str(first(found.get("name"), name))), str(first(found.get("ecosystem"), ecosystem)), "latest %s" % found["latest"] if found.get("latest") else ""]
    lines = ["", "  %s" % "  ".join(part for part in head if part)]
    span = " from %s to %s" % (since, to) if since and to else " since %s" % since if since else ""
    lines.extend(["  %s%s%s" % (
        text.plural(len(changes), "change") if changes else "No changes recorded",
        span,
        ", and more" if answer["json"].get("nextCursor") else "",
    ), ""])
    lines.extend(text.change_lines(changes, c))
    if changes:
        lines.append("")
    # A move (--to) lists its releases and how each was read: what Since has and has not read, never a verdict on the move.
    releases = answer["json"]["releases"] if to and isinstance(answer["json"].get("releases"), dict) else None
    more = False
    if releases is not None:
        moved, more = text.release_lines(releases, since, to, show_all)
        lines.extend(moved + [""])
    notes = []
    if answer["json"].get("reading"):
        notes.append("Since is reading %s now. Run this again in a few minutes to include them." % ("some of these releases" if to else "newer releases of this package"))
    for note in notes:
        lines.append("  %s" % note)
    if notes:
        lines.append("")
    if answer["json"].get("nextCursor") or more:
        flags = "%s%s%s" % (" --since %s" % since if since else "", " --to %s" % to if to else "", " --ecosystem %s" % ecosystem if ecosystem != "npm" else "")
        lines.extend(["  %s%s changed %s%s --all" % ("More".ljust(14), p, name, flags), ""])
    io.out("\n".join(lines))
    return 0


def login_command(ctx):
    """Sign in without a password: the person approves the link in a browser and the key comes back."""
    io, args, p = ctx["io"], ctx["args"], ctx["p"]
    email = str(first(args["flags"].get("email"), "")).strip()
    if not email and io.tty:
        email = str(io.prompt("Email: ")).strip()
    if not email:
        io.err("Give the email to sign in with: %s login --email you@example.com\n" % p)
        return 2
    start = call(ctx, "POST", "/v1/agent/register", body={"email": email, "client_name": "since-cli"})
    setup = start["json"].get("setup") if start["json"] is not None and isinstance(start["json"].get("setup"), dict) else None
    if start["status"] != 200 or setup is None or not isinstance(start["json"].get("claim_token"), str):
        return refuse(ctx, start)
    link = str(first(setup.get("verification_uri_complete"), setup.get("verification_uri")))
    # The approval page shows an eight-character code in two halves: print it the way the person will see it there.
    code = str(first(setup.get("user_code"), ""))
    io.out("\n  Open this link to approve the sign-in:\n    %s\n  Code: %s\n\n" % (link, "%s-%s" % (code[:4], code[4:]) if len(code) == 8 else code))
    if io.tty:
        io.open(link)
    interval = clamp(first(whole_number(setup.get("interval")), 5), 1, 60)
    deadline = io.now() + clamp(first(whole_number(setup.get("expires_in")), 900), 30, 3600) * 1000
    while io.now() + interval * 1000 <= deadline:
        io.sleep(interval * 1000)
        answer = call(ctx, "POST", "/v1/agent/token", body={"claim_token": start["json"]["claim_token"]})
        if answer["status"] == 200 and answer["json"] is not None and isinstance(answer["json"].get("api_key"), str):
            file = credentials_path(io)
            stored = {"api": ctx["api"], "api_key": answer["json"]["api_key"], "key_name": answer["json"].get("key_name"), "key_prefix": answer["json"].get("key_prefix")}
            io.write_file(file, "%s\n" % dumps(stored, 2), 0o600)
            io.out("  Signed in. The key is stored in %s\n\n" % file)
            return 0
        code = error_of(answer)
        if code == "slow_down":
            interval += 5
        elif code == "expired_token":
            break
        elif code == "access_denied":
            io.err("The sign-in was declined.\n")
            return 1
        elif code != "authorization_pending" and answer["status"] != 0 and answer["status"] < 500:
            return refuse(ctx, answer)
    io.err("The link expired before it was approved. Run %s login again.\n" % p)
    return 1


def door_words(watch):
    """How a watch arrived, as the list says it: the API, the CLI, or MCP, with the client that made it when known."""
    if watch.get("door") == "cli":
        return "CLI"
    client = watch.get("client")
    return "%s%s" % (str(first(watch.get("door"), "api")).upper(), " (%s)" % client if isinstance(client, str) and client else "")


def watches_command(ctx):
    """`since watches`: what this workspace watches, and how to stop each."""
    io, args, p, c = ctx["io"], ctx["args"], ctx["p"], ctx["c"]
    if not ctx["key"]:
        io.err("%s watches needs a key. Sign in first: %s login\n" % (p, p))
        return 1
    answer = call(ctx, "GET", "/v1/watches", key=ctx["key"])
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("watches"), list):
        return refuse(ctx, answer)
    if args["flags"].get("json"):
        io.out("%s\n" % dumps(answer["json"], 2))
        return 0
    watches = answer["json"]["watches"]
    ended = answer["json"]["ended"] if isinstance(answer["json"].get("ended"), list) else []
    indent = " " * 16
    active = first(whole_number(answer["json"].get("active")), len(watches))
    limit = first(whole_number(answer["json"].get("limit")), 100)
    lines = ["", "  %s  %d of %d" % (c.bold("Watches"), active, limit), ""]
    if not watches:
        lines.extend(["  Nothing watched yet. Add --watch to %s check, or watch: true to a check." % p, ""])
    for watch in watches:
        name = watch.get("name")
        lines.append("  %s%s" % (watch.get("subject"), "  %s" % name if isinstance(name, str) and name else ""))
        lines.append("%s%s" % (indent, "  ".join([str(watch.get("input")), door_words(watch), "%s watched" % text.num(first(whole_number(watch.get("watchedCount")), 0)),
                                                   "until %s" % str(first(watch.get("expiresAt"), ""))[:10]])))
        sent = watch["sent"] if isinstance(watch.get("sent"), list) else []
        count = first(whole_number(watch.get("sentCount")), len(sent))
        lines.append("%s%s" % (indent, "Found %s: %s" % (text.num(count), sent[0].get("headline")) if count and sent else "Nothing new found yet"))
        lines.extend(["%sRemove: %s watches remove %s" % (indent, p, watch.get("id")), ""])
    if ended:
        lines.extend(["  Ended in the last 30 days: %s. What each kept is deleted 30 days after it ended." % text.num(len(ended)), ""])
    io.out("\n".join(lines))
    return 0


def remove_watch_command(ctx, watch_id):
    """`since watches remove <id>`: stop watching one."""
    io, p = ctx["io"], ctx["p"]
    if not ctx["key"]:
        io.err("%s watches needs a key. Sign in first: %s login\n" % (p, p))
        return 1
    answer = call(ctx, "DELETE", "/v1/watches/%s" % quote(watch_id, safe=""), key=ctx["key"])
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("subject"), str):
        return refuse(ctx, answer)
    name = answer["json"].get("name")
    io.out("Stopped watching %s%s. What it kept is deleted on %s.\n" % (answer["json"]["subject"], " (%s)" % name if isinstance(name, str) and name else "",
                                                                       str(first(answer["json"].get("deletedForGoodAt"), ""))[:10]))
    return 0


def logout_command(ctx):
    io = ctx["io"]
    removed = io.remove(credentials_path(io))
    io.out("Signed out. The key is gone from this computer; delete it for good at %s/api-keys\n" % APP if removed else "No stored key.\n")
    return 0


def run(argv, io):
    args = parse_args(argv)
    p = io.program

    def usage(message):
        io.err("%s\nRun %s --help for the commands.\n" % (message, p))
        return 2

    if args["error"]:
        return usage(args["error"])
    flags = args["flags"]
    if flags.get("version") or args["command"] == "version":
        io.out("since-dev %s\n" % VERSION)
        return 0
    if flags.get("help") or args["command"] == "help":
        io.out(help_text(p))
        return 0
    command = args["command"]
    words = args["positional"]
    if command == "changed":
        if len(words) != 1:
            return usage("Too many words: %s" % words[1] if words else "%s changed needs a package name, as %s changed react" % (p, p))
        if "to" in flags and "since" not in flags:
            return usage("--to needs --since, the version you move from.")
    elif command == "watches":
        if words and words[0] != "remove":
            return usage("Unknown command: watches %s" % words[0])
        if words and len(words) != 2:
            return usage("Too many words: %s" % words[2] if len(words) > 2 else "%s watches remove needs a watch's id, as %s watches lists them." % (p, p))
    elif command == "hook":
        if len(words) != 1:
            return usage("Too many words: %s" % words[1] if len(words) > 1 else "%s hook needs the hook's event, as %s hook post-tool-use" % (p, p))
    elif command is None or command == "check":
        if len(words) > 1:
            return usage("Too many words: %s" % words[1])
        if command is None and len(words) == 1 and "/" not in words[0]:
            return usage("Unknown command: %s" % words[0])
    elif words:
        return usage("Too many words: %s" % words[0])

    fail_on = first(flags.get("fail-on"), "high")
    if fail_on not in FAIL_ON:
        return usage("--fail-on takes low, medium, high, critical or none.")
    within = None
    if "within" in flags:
        if not re.fullmatch(r"[0-9]{1,4}d?", flags["within"]) or int(flags["within"].rstrip("d")) < 1:
            return usage("--within takes a number of days, as 30d.")
        within = int(flags["within"].rstrip("d"))
    max_files = 50
    if "max-files" in flags:
        if not re.fullmatch(r"[0-9]{1,3}", flags["max-files"]) or int(flags["max-files"]) < 1:
            return usage("--max-files takes a number from 1 to 999.")
        max_files = int(flags["max-files"])

    api = re.sub(r"/+$", "", str(io.env.get("SINCE_API_URL") or DEFAULT_API))
    color = bool(io.tty) and not flags.get("no-color") and not io.env.get("NO_COLOR") and io.env.get("TERM") != "dumb"
    ctx = {
        "io": io, "args": args, "p": p, "api": api, "c": text.Palette(color), "check": command == "check",
        "key": read_key(io, api), "fail_on": fail_on, "within": within, "max_files": max_files,
    }
    if command == "hook":
        return hook_command(ctx, words[0])
    if command == "calendar":
        return calendar_command(ctx)
    if command == "changed":
        return changed_command(ctx)
    if command == "init":
        return init(io, p, dry_run=bool(flags.get("dry-run")), yes=bool(flags.get("yes")), hook=bool(flags.get("hook")), pre_commit=bool(flags.get("pre-commit")))
    if command == "login":
        return login_command(ctx)
    if command == "logout":
        return logout_command(ctx)
    if command == "watches":
        return remove_watch_command(ctx, words[1]) if words else watches_command(ctx)
    return repo_command(ctx)
