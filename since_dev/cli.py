"""The whole CLI: run(argv, io) returns the exit code. Everything it touches (network, files, clock, terminal) comes in
through io, so the tests replay it from fixtures, and src/cli.mjs prints the same text from the same fixtures.

Exit codes: 0 done. `check`: 1 when a finding fails, 2 when the caller has something to fix, 0 when Since could not be
reached (an outage never fails a build). Every other command: 1 when it could not finish, 2 for a usage error.
"""

import datetime
import json
import re
from urllib.parse import quote

from . import __version__ as VERSION
from . import render as text
from .init import dumps, init, loads
from .local import MAX_CHARACTERS, check_body, find_manifests, parse_repo

DEFAULT_API = "https://api.since.dev"
SITE = "https://since.dev"
APP = "https://app.since.dev"
COMMANDS = ["check", "calendar", "changed", "init", "login", "logout", "help", "version"]
SWITCHES = ["all", "json", "local", "dry-run", "no-color", "help", "version"]
OPTIONS = ["fail-on", "within", "provider", "ecosystem", "since", "email", "max-files"]
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
    ]
    options = [
        ["--all", "show every finding"],
        ["--json", "print the answer as JSON"],
        ["--local", "check the manifests in this folder, not GitHub"],
        ["--fail-on <level>", "check: low, medium, high (default), critical or none"],
        ["--within <days>", "only dates within this many days, as 30d"],
        ["--provider <name>", "calendar: one provider, as openai"],
        ["--ecosystem <name>", "changed: the registry, as npm (default) or pypi"],
        ["--since <version>", "changed: only what changed after this version"],
        ["--email <address>", "login: the email to sign in with"],
        ["--dry-run", "init: show what would be written"],
        ["--max-files <n>", "how many manifests to check (default 10)"],
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


def call(ctx, method, path, query=None, body=None, key=None):
    """One request. status 0 means Since could not be reached; json is the answer when it is a JSON object."""
    search = encode_query(query) if query else ""
    headers = {"User-Agent": "since-cli/%s (%s)" % (VERSION, ctx["io"].runtime), "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer %s" % key["value"]
    try:
        answer = ctx["io"].fetch(method, "%s%s%s" % (ctx["api"], path, "?" + search if search else ""), headers, None if body is None else json.dumps(body))
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
    """The free scan of a public GitHub repository. A busy answer is waited out up to three times."""
    waits = 0
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


def local_check(ctx, no_origin):
    """The manifests in this folder, sent one by one as the GitHub Action sends them."""
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
    files = []
    notes = []
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
            answer = check_all(ctx, check_body(file, content))
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
        whole = data["result"] if data["kind"] == "scan" else data["answer"] if data["kind"] == "repo" else {"files": data["files"]}
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
        head = [c.bold(str(repository.get("fullName"))), "%s@%s" % (branch, sha) if sha else branch, "scanned %s" % scanned if scanned else ""]
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
        head = [c.bold(str(data["repo"].get("fullName"))), str(first(data["repo"].get("defaultBranch"), "")), "connected repository"]
        lines.append("  %s" % "  ".join(part for part in head if part))
        lines.append("  %s   %s checked" % (counts, text.num(first(whole_number(data["answer"].get("checkedCount")), 0))))
        notes.extend(text.answer_notes([data["answer"]]))
    else:
        checked = sum(first(whole_number(file["answer"].get("checkedCount")), 0) for file in data["files"])
        lines.append("  %s  %s checked" % (c.bold("This folder"), text.plural(len(data["files"]), "manifest")))
        lines.append("  %s   %s checked" % (counts, text.num(checked)))
        notes.extend(data["notes"])
        notes.extend(text.answer_notes([file["answer"] for file in data["files"]]))
        notes.append("Only manifests were checked. Connect the repository to check its code too: %s?utm_source=cli" % APP)
    lines.append("")
    lines.extend(text.finding_lines(items, show_all, c, more))
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
    answer = call(ctx, "GET", "/v1/changes", query={"package": name, "ecosystem": ecosystem, "since": since, "limit": 200 if args["flags"].get("all") else 20}, key=ctx["key"])
    if answer["status"] != 200 or answer["json"] is None or not isinstance(answer["json"].get("changes"), list):
        return refuse(ctx, answer)
    if args["flags"].get("json"):
        io.out("%s\n" % dumps(answer["json"], 2))
        return 0
    changes = text.unique_changes(answer["json"]["changes"])
    found = answer["json"]["package"] if isinstance(answer["json"].get("package"), dict) else {}
    head = [c.bold(str(first(found.get("name"), name))), str(first(found.get("ecosystem"), ecosystem)), "latest %s" % found["latest"] if found.get("latest") else ""]
    lines = ["", "  %s" % "  ".join(part for part in head if part)]
    lines.extend(["  %s%s%s" % (
        text.plural(len(changes), "change") if changes else "No changes recorded",
        " since %s" % since if since else "",
        ", and more" if answer["json"].get("nextCursor") else "",
    ), ""])
    lines.extend(text.change_lines(changes, c))
    if changes:
        lines.append("")
    notes = []
    if answer["json"].get("reading"):
        notes.append("Since is reading newer releases of this package now. Run this again in a few minutes to include them.")
    for note in notes:
        lines.append("  %s" % note)
    if notes:
        lines.append("")
    if answer["json"].get("nextCursor"):
        lines.extend(["  %s%s changed %s%s%s --all" % ("More".ljust(14), p, name, " --since %s" % since if since else "", " --ecosystem %s" % ecosystem if ecosystem != "npm" else ""), ""])
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
    max_files = 10
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
    if command == "calendar":
        return calendar_command(ctx)
    if command == "changed":
        return changed_command(ctx)
    if command == "init":
        return init(io, p, bool(flags.get("dry-run")))
    if command == "login":
        return login_command(ctx)
    if command == "logout":
        return logout_command(ctx)
    return repo_command(ctx)
