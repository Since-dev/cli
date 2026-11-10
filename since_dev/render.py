"""Text only: every function here turns an API answer into lines. No network, no files, no clock.

src/render.mjs is the same code in JavaScript, and the shared fixtures hold both to the same output.
"""

SEVERITIES = ["low", "medium", "high", "critical"]
PAD = " " * 12
SHOWN = 10


class Palette:
    def __init__(self, on):
        self.on = on

    def _paint(self, code, text):
        return "\x1b[%dm%s\x1b[0m" % (code, text) if self.on else text

    def bold(self, text):
        return self._paint(1, text)

    def dim(self, text):
        return self._paint(2, text)

    def red(self, text):
        return self._paint(31, text)

    def yellow(self, text):
        return self._paint(33, text)

    def plain(self, text):
        return text


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def num(value):
    return "{:,}".format(value)


def plural(count, word):
    return "%s %s%s" % (num(count), word, "" if count == 1 else "s")


def wait(seconds):
    """How long to wait, in words: seconds under a minute and a half, then minutes, then hours, rounded up."""
    if seconds < 90:
        return plural(max(1, seconds), "second")
    if seconds < 5400:
        return plural(-(-seconds // 60), "minute")
    return plural(-(-seconds // 3600), "hour")


def ago(ms):
    """How long ago, from milliseconds; "" when the time is not known."""
    if ms is None:
        return ""
    minutes = ms // 60000
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return "%d min ago" % minutes
    hours = minutes // 60
    if hours < 48:
        return "%s ago" % plural(hours, "hour")
    return "%s ago" % plural(hours // 24, "day")


def _place_of(path, line):
    return "%s:%d" % (path, line) if is_number(line) and line > 0 else str(path)


def _join(parts, separator):
    return separator.join(str(part) for part in parts if part)


def scan_items(findings):
    """A public scan's findings, as the rows the CLI prints."""
    items = []
    for finding in findings:
        where = finding.get("where")
        where = [place for place in where if isinstance(place, dict) and place.get("path")] if isinstance(where, list) else []
        source = finding.get("source")
        source = source.get("url") if isinstance(source, dict) else None
        title = finding.get("title")
        items.append({
            "title": "" if title is None else str(title),
            "review": finding.get("state") == "needs_review",
            "severity": finding.get("severity"),
            "daysLeft": finding.get("daysLeft") if is_number(finding.get("daysLeft")) else None,
            "place": _place_of(where[0]["path"], where[0].get("line")) if where else None,
            "extra": max(0, len(where) - 1) + (finding["more"] if is_number(finding.get("more")) else 0),
            "replacement": finding.get("replacement") or None,
            "source": source or None,
        })
    return items


def _merge_rows(group, program):
    """Many undated findings about one dependency at one place, as one row: the count, the worst severity, and how to list them."""
    members = group["members"]
    proven = [row for row in members if not row["review"]]
    pool = proven or members

    def rank(level):
        return SEVERITIES.index(level) if level in SEVERITIES else -1

    severity = pool[0]["severity"]
    for row in pool:
        if rank(row["severity"]) > rank(severity):
            severity = row["severity"]
    tally = [[level, len([row for row in members if row["severity"] == level])] for level in reversed(SEVERITIES)]
    tally = [entry for entry in tally if entry[1]]
    unrated = len(members) - sum(entry[1] for entry in tally)
    if unrated:
        tally.append(["unrated", unrated])
    breakdown = " (%s)" % ", ".join("%s %s" % (num(count), level) for level, count in tally) if len(tally) > 1 else ""
    since = " --since %s" % group["version"] if group["version"] else ""
    registry = " --ecosystem %s" % group["ecosystem"] if group["ecosystem"] and group["ecosystem"] != "npm" else ""
    return {
        "title": "%s: %s changes between this version and the latest%s" % (group["subject"], num(len(members)), breakdown),
        "review": not proven,
        "severity": severity,
        "daysLeft": None,
        "place": members[0]["place"],
        "extra": 0,
        "replacement": None,
        "source": None,
        "hint": "%s changed %s%s%s" % (program, group["name"], since, registry) if group["isPackage"] and group["name"] else None,
    }


def check_items(findings, fallback_path, program):
    """A keyed check's findings as the same rows. They carry no title, so one is built from the name, the version and the reason.

    A manifest check answers with every recorded change between the pinned version and the latest, hundreds for an old pin:
    the undated ones about one dependency at one place become one row, and `since changed` lists them.
    """
    rows = []
    groups = {}
    for finding in findings:
        surface = finding["surface"] if isinstance(finding.get("surface"), dict) else {}
        name = str(surface.get("name") or "")
        version = finding.get("version") or None
        subject = _join([name, version], " ")
        reason = finding.get("reason")
        reason = ("" if reason is None else str(reason)).strip()
        where = finding.get("where")
        where = [place for place in where if isinstance(place, dict) and place.get("file")] if isinstance(where, list) else []
        first = _place_of(where[0]["file"], where[0].get("line")) if where else fallback_path
        source = finding.get("source")
        if isinstance(source, dict):
            source = source.get("url")
        places = finding["placeCount"] if is_number(finding.get("placeCount")) else len(where)
        row = {
            "title": reason if name and name.lower() in reason.lower() else _join([subject, reason], ": "),
            "review": finding.get("state") == "needs_review",
            "severity": finding.get("severity"),
            "daysLeft": finding.get("daysLeft") if is_number(finding.get("daysLeft")) else None,
            "place": first or None,
            "extra": max(0, places - 1),
            "replacement": finding.get("replacement") or None,
            "source": source if isinstance(source, str) and source else None,
            "hint": None,
        }
        if row["daysLeft"] is not None:
            rows.append(row)
            continue
        key = "%s|%s|%s" % (surface.get("key") or name, version or "", row["place"] or "")
        if key in groups:
            groups[key]["members"].append(row)
        else:
            groups[key] = {"members": [row], "subject": subject, "name": name, "version": version, "isPackage": surface.get("kind") == "package", "ecosystem": surface.get("ecosystem") or None}
            rows.append(groups[key])
    return [(row["members"][0] if len(row["members"]) == 1 else _merge_rows(row, program)) if "members" in row else row for row in rows]


def order(items):
    """Dated findings first (the next deadline, then the ones already passed, newest first), then undated, then the ones to review."""
    def group(item):
        return 2 if item["review"] else 1 if item["daysLeft"] is None else 0

    def within(item):
        if group(item) != 0:
            return 0
        return item["daysLeft"] if item["daysLeft"] >= 0 else 100000 - item["daysLeft"]

    return sorted(items, key=lambda item: (group(item), within(item)))


def fails(item, fail_on):
    """The CI rule, the same as the GitHub Action's: proven, and at or above the severity asked for."""
    if fail_on not in SEVERITIES or item["review"] or item["severity"] not in SEVERITIES:
        return False
    return SEVERITIES.index(item["severity"]) >= SEVERITIES.index(fail_on)


def day_label(days_left):
    if days_left < 0:
        return "passed"
    if days_left == 0:
        return "today"
    return "1 day" if days_left == 1 else "%s days" % num(days_left)


def _label(item):
    if item["review"]:
        return "review"
    if item["daysLeft"] is None:
        return item["severity"] if item["severity"] in SEVERITIES else "no date"
    return day_label(item["daysLeft"])


def _day_tone(days_left, c):
    return c.red if days_left <= 30 else c.yellow if days_left <= 90 else c.plain


def _tone(item, c):
    if item["review"]:
        return c.dim
    if item["daysLeft"] is None:
        return c.yellow if item["severity"] in ("critical", "high") else c.plain
    return _day_tone(item["daysLeft"], c)


def counts_line(items, within):
    """"3 with a date   2 undated   1 to review", without the counts that are zero."""
    dated = len([item for item in items if not item["review"] and item["daysLeft"] is not None])
    undated = len([item for item in items if not item["review"] and item["daysLeft"] is None])
    review = len([item for item in items if item["review"]])
    parts = []
    if dated:
        parts.append("%s with a date" % num(dated))
    if undated:
        parts.append("%s undated" % num(undated))
    if review:
        parts.append("%s to review" % num(review))
    if parts:
        return "   ".join(parts)
    return "No findings" if within is None else "Nothing dated within %s" % plural(within, "day")


def finding_lines(items, show_all, c, more_hint):
    """The findings, three lines each: when and what, where and the replacement, the source."""
    lines = []
    shown = items if show_all else items[:SHOWN]
    for item in shown:
        lines.append("  %s%s" % (_tone(item, c)(_label(item).ljust(10)), item["title"]))
        place = item["place"] + ("  and %s more" % num(item["extra"]) if item["extra"] else "") if item["place"] else None
        detail = _join([place, "use %s" % item["replacement"] if item["replacement"] else None, "list them: %s" % item["hint"] if item.get("hint") else None], "   ")
        if detail:
            lines.append(c.dim(PAD + detail))
        if item["source"]:
            lines.append(c.dim(PAD + item["source"]))
        lines.append("")
    if len(items) > len(shown):
        lines.extend(["  and %s more   %s" % (num(len(items) - len(shown)), more_hint), ""])
    return lines


def _count(value, fallback):
    return int(value) if is_number(value) and value else fallback


def answer_notes(answers):
    """What keyed answers say about their own gaps: still being read, cut short, or names that did not resolve."""
    notes = []
    scanning = 0
    reading = False
    releases = 0
    truncated = False
    unresolved = []
    for answer in answers:
        if isinstance(answer.get("scanning"), dict):
            scanning = max(scanning, _count(answer["scanning"].get("retryAfterSec"), 60))
        if isinstance(answer.get("reading"), dict):
            reading = True
            releases += _count(answer["reading"].get("releases"), 0)
        if answer.get("truncated"):
            truncated = True
        if isinstance(answer.get("unresolved"), list):
            unresolved.extend(answer["unresolved"])
    if scanning:
        notes.append("Since is still reading this repository. Run this again in %s." % wait(scanning))
    if reading:
        what = plural(releases, "newer release") if releases else "newer releases"
        notes.append("Since is reading %s now. Run this again in a few minutes to include them." % what)
    if truncated:
        notes.append("The answer was cut short: there are more findings than one answer holds.")
    if unresolved:
        names = ", ".join(str(entry.get("name")) for entry in unresolved[:3])
        notes.append("Not fully checked yet: %s%s. --json says why." % (names, " and %s more" % num(len(unresolved) - 3) if len(unresolved) > 3 else ""))
    return notes


def calendar_lines(entries, site, c):
    """One line per calendar entry, with its replacement and its page under it."""
    lines = []
    for entry in entries:
        days = entry.get("daysLeft") if is_number(entry.get("daysLeft")) else None
        when = "no date" if days is None else day_label(days)
        paint = c.plain if days is None else _day_tone(days, c)
        lines.append("  %s%s" % (paint(when.ljust(10)), entry.get("title")))
        path = entry.get("path")
        link = "%s%s?utm_source=cli" % (site, path) if isinstance(path, str) and path.startswith("/") else None
        detail = _join(["use %s" % entry["replacement"] if entry.get("replacement") else None, link], "   ")
        if detail:
            lines.append(c.dim(PAD + detail))
    return lines


def unique_changes(changes):
    # One change can be recorded once per function it touches, with the same words: say it once.
    seen = set()
    unique = []
    for change in changes:
        key = (change.get("kind"), change.get("title"), change.get("introducedIn") or change.get("removedIn"))
        if key not in seen:
            seen.add(key)
            unique.append(change)
    return unique


def change_lines(changes, c):
    """One line per change of a package, with the version, the date and the source under it."""
    lines = []
    for change in changes:
        kind = change.get("kind")
        version = change.get("introducedIn") or change.get("removedIn")
        lines.append("  %s%s" % (("" if kind is None else str(kind)).replace("_", " ").ljust(19), change.get("title")))
        evidence = change.get("evidence")
        evidence = evidence[0].get("url") if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict) else None
        detail = _join(["in %s" % version if version else None, change.get("effectiveDate") or None, evidence or None], "   ")
        if detail:
            lines.append(c.dim(" " * 21 + detail))
    return lines
