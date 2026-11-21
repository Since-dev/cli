"""What the CLI reads from the folder it runs in: which repository this is, and which manifests to send.

The manifest rules are the GitHub Action's: the same names, the same folders skipped.
"""

import re

PATTERNS = [
    "**/package.json",
    "**/requirements*.txt",
    "**/pyproject.toml",
    "**/go.mod",
    "**/Cargo.toml",
    "**/Gemfile",
    "**/composer.json",
    "**/pom.xml",
    "**/build.gradle",
    "**/build.gradle.kts",
    "**/*.csproj",
]
LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
    "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
    "packages.lock.json", "gradle.lockfile",
}
SKIP = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "target", ".next", "__pycache__"}
MAX_CHARACTERS = 500000

_SHORT = re.compile(r"([A-Za-z0-9_-][A-Za-z0-9_.-]*)/([A-Za-z0-9_.-]+)")
_REMOTE = re.compile(
    r"(?:https?://|ssh://|git://)?(?:[^@/]+@)?([A-Za-z0-9.-]+\.[A-Za-z]+)(?::[0-9]+)?[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?"
)


def parse_repo(text):
    """owner and name from "owner/repo", a GitHub URL or a git remote; None for anything else. A bare "owner/repo" means GitHub."""
    value = ("" if text is None else str(text)).strip()
    if not value:
        return None
    short = _SHORT.fullmatch(value)
    if short:
        name = short.group(2)
        return {"host": "github.com", "owner": short.group(1), "name": name[:-4] if name.endswith(".git") else name}
    remote = _REMOTE.fullmatch(value)
    if not remote:
        return None
    return {"host": remote.group(1).lower(), "owner": remote.group(2), "name": remote.group(3)}


def pattern_regexp(pattern):
    parts = re.split(r"(\*\*/|\*)", re.sub(r"^\./", "", pattern))
    return re.compile("".join("(?:.*/)?" if part == "**/" else "[^/]*" if part == "*" else re.escape(part) for part in parts))


def find_manifests(io):
    """The manifests under the folder, nearest the top first, then by name."""
    patterns = [pattern_regexp(pattern) for pattern in PATTERNS]
    found = []

    def walk(folder):
        for entry in io.list("%s/%s" % (io.cwd, folder) if folder else io.cwd):
            relative = "%s/%s" % (folder, entry["name"]) if folder else entry["name"]
            if entry["dir"]:
                if entry["name"] not in SKIP:
                    walk(relative)
            elif any(pattern.fullmatch(relative) for pattern in patterns):
                found.append(relative)

    walk("")
    return sorted(found, key=lambda file: (file.count("/"), file))


MAX_SET_FILES = 50
"""The most files one set holds: the API's own limits on `files`."""


def is_lockfile(file):
    """Whether a file is a lockfile, by its name."""
    return file.split("/")[-1] in LOCKFILES


def files_to_send(io, manifests):
    """What a local check sends, in order: each manifest, nearest the top first, followed by the lockfiles in its own folder, so the
    versions read are the installed ones. Each file once."""
    out = []
    seen = set()

    def add(file):
        if file not in seen:
            seen.add(file)
            out.append(file)

    for manifest in manifests:
        add(manifest)
        folder = manifest.rsplit("/", 1)[0] if "/" in manifest else ""
        names = sorted(entry["name"] for entry in io.list("%s/%s" % (io.cwd, folder) if folder else io.cwd) if not entry["dir"] and entry["name"] in LOCKFILES)
        for name in names:
            add("%s/%s" % (folder, name) if folder else name)
    return out


def file_sets(files):
    """The files in sets the API takes, in order: up to 50 files and 500,000 characters each; a file that would overflow a set starts
    the next."""
    sets = []
    current = []
    size = 0
    for file in files:
        if current and (len(current) >= MAX_SET_FILES or size + len(file["content"]) > MAX_CHARACTERS):
            sets.append(current)
            current = []
            size = 0
        current.append(file)
        size += len(file["content"])
    if current:
        sets.append(current)
    return sets


def check_body(file, content):
    name = file.split("/")[-1]
    return {"lockfile" if name in LOCKFILES else "manifest": {"path": file, "content": content}, "detail": "summary"}
