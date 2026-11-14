"""python3 -m unittest test_cli

Replays every fixture through the Python package with a fake network, folder and clock, and compares what it printed,
what it requested and what it wrote with expected/<name>.txt: the same files test.mjs holds the npm package to.
"""

import json
import os
import re
import unittest

from since_dev import __version__
from since_dev.cli import parse_args, run
from since_dev.init import BLOCK, CLAUDE_RULE, CURSOR_RULE, MCP_URL, RULE, RULES, TARGETS, merge_config, merge_rule, merge_whole, said_yes
from since_dev.local import PATTERNS, check_body, parse_repo, pattern_regexp
from since_dev.render import ago, wait

HERE = os.path.dirname(os.path.abspath(__file__))
CWD = "/repo"
HOME = "/home/dev"
API = "https://api.since.dev"


def canonical(value):
    """JSON with sorted keys and no spaces: the form both languages can print alike."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as handle:
        return handle.read()


class FakeIo:
    runtime = "python"
    cwd = CWD
    home = HOME
    platform = "linux"

    def __init__(self, fixture):
        self.fixture = fixture
        self.program = fixture.get("program", "since")
        self.env = fixture.get("env", {})
        self.tty = bool(fixture.get("tty"))
        self.files = {}
        for name, content in fixture.get("files", {}).items():
            self.files[name if name.startswith("/") else "%s/%s" % (CWD, name)] = content
        self.printed = []
        self.errors = []
        self.log = []
        self.wrote = []
        self.answers = list(fixture.get("http", []))
        self.clock = _milliseconds(fixture["now"])

    def out(self, text):
        self.printed.append(text)

    def err(self, text):
        self.errors.append(text)

    def now(self):
        return self.clock

    def sleep(self, ms):
        self.log.append("sleep %d" % ms)
        self.clock += ms

    def fetch(self, method, url, headers, body):
        assert url.startswith(API + "/"), url
        assert headers["User-Agent"] == "since-cli/%s (python)" % __version__, headers["User-Agent"]
        address = url[len(API):]
        parts = ["%s %s" % (method, address), "key" if "Authorization" in headers else None, None if body is None else canonical(json.loads(body))]
        self.log.append(" ".join(part for part in parts if part))
        if "Authorization" in headers:
            assert headers["Authorization"] == "Bearer fixture-key"
        assert self.answers, "no answer left for %s %s" % (method, address)
        following = self.answers.pop(0)
        assert "%s %s" % (method, address.split("?")[0]) == following["request"], following["request"]
        if following.get("fail"):
            raise OSError("network down")
        return {"status": following["status"], "retryAfter": following.get("retryAfter"), "text": json.dumps(following["body"])}

    def origin(self):
        return self.fixture.get("origin")

    def root(self):
        return CWD

    def open(self, url):
        self.log.append("open %s" % url)

    def prompt(self, question):
        self.log.append("prompt %s" % question.strip())
        return self.fixture.get("answer", "")

    def read_file(self, file):
        return self.files.get(file)

    def write_file(self, file, text, mode):
        self.files[file] = text
        name = file[len(CWD) + 1:] if file.startswith(CWD + "/") else file
        self.wrote.append("--- wrote %s%s\n%s" % (name, "" if mode is None else " mode %o" % mode, text))

    def remove(self, file):
        if file not in self.files:
            return False
        del self.files[file]
        self.log.append("remove %s" % file)
        return True

    def list(self, folder):
        entries = {}
        for file in self.files:
            if not file.startswith(folder + "/"):
                continue
            rest = file[len(folder) + 1:].split("/")
            entries[rest[0]] = len(rest) > 1
        return [{"name": name, "dir": entries[name]} for name in sorted(entries)]


def _milliseconds(iso):
    from since_dev.cli import parse_time
    return parse_time(iso)


def replay(fixture):
    io = FakeIo(fixture)
    code = run(fixture["args"], io)
    assert not io.answers, "every prepared answer is asked for"
    transcript = "$ %s\n%s" % (" ".join(["since"] + fixture["args"]), "".join(io.printed))
    if io.errors:
        transcript += "--- stderr\n%s" % "".join(io.errors)
    if io.log:
        transcript += "--- log\n%s\n" % "\n".join(io.log)
    transcript += "".join(io.wrote)
    return "%s--- exit %d\n" % (transcript, code)


NAMES = sorted(name[:-5] for name in os.listdir(os.path.join(HERE, "fixtures")) if name.endswith(".json"))


class Fixtures(unittest.TestCase):
    maxDiff = None

    def test_every_fixture_prints_what_the_npm_package_prints(self):
        for name in NAMES:
            with self.subTest(name):
                self.assertEqual(replay(json.loads(read("fixtures", "%s.json" % name))), read("expected", "%s.txt" % name))

    def test_every_expected_file_has_its_fixture(self):
        self.assertEqual(sorted(name[:-4] for name in os.listdir(os.path.join(HERE, "expected"))), NAMES)


class Pieces(unittest.TestCase):
    def test_a_repository_is_read_from_every_form_of_name_and_remote(self):
        github = {"host": "github.com", "owner": "acme", "name": "shop"}
        for form in [
            "acme/shop",
            "acme/shop.git",
            "github.com/acme/shop",
            "https://github.com/acme/shop",
            "https://github.com/acme/shop.git",
            "https://github.com/acme/shop/tree/main/src",
            "https://token@github.com/acme/shop.git",
            "git@github.com:acme/shop.git",
            "ssh://git@github.com:22/acme/shop.git",
            "  git@github.com:acme/shop  ",
        ]:
            self.assertEqual(parse_repo(form), github, form)
        self.assertEqual(parse_repo("git@gitlab.com:acme/shop.git"), {"host": "gitlab.com", "owner": "acme", "name": "shop"})
        self.assertEqual(parse_repo("https://github.com/acme/shop.js"), {"host": "github.com", "owner": "acme", "name": "shop.js"})
        for form in ["", "shop", "../shop", "/srv/git/shop.git", "file:///srv/git/shop.git", "C:\\code\\shop", None]:
            self.assertIsNone(parse_repo(form), form)

    def test_manifests_match_the_patterns_the_action_uses(self):
        self.assertTrue(pattern_regexp("**/package.json").fullmatch("package.json"))
        self.assertTrue(pattern_regexp("**/package.json").fullmatch("apps/web/package.json"))
        self.assertFalse(pattern_regexp("**/package.json").fullmatch("apps/web/package.json5"))
        self.assertTrue(pattern_regexp("**/requirements*.txt").fullmatch("api/requirements-dev.txt"))
        self.assertTrue(pattern_regexp("**/*.csproj").fullmatch("src/Api/Api.csproj"))
        self.assertEqual(check_body("api/go.mod", "module x"), {"manifest": {"path": "api/go.mod", "content": "module x"}, "detail": "summary"})
        self.assertEqual(list(check_body("api/uv.lock", "")), ["lockfile", "detail"])
        self.assertEqual(list(check_body("go.sum", "")), ["manifest", "detail"])

    def test_arguments(self):
        self.assertEqual(parse_args(["check", "--fail-on=low", "--within", "30d", "--json"])["flags"], {"fail-on": "low", "within": "30d", "json": True})
        self.assertIsNone(parse_args(["acme/shop", "--all"])["command"])
        self.assertEqual(parse_args(["--email"])["error"], "--email needs a value.")
        self.assertEqual(parse_args(["--verbose"])["error"], "Unknown option: --verbose")
        self.assertEqual(parse_args(["--all=yes"])["error"], "Unknown option: --all=yes")

    def test_waits_and_ages_read_as_words(self):
        self.assertEqual([wait(s) for s in [1, 20, 89, 90, 3600, 5400, 32400]], ["1 second", "20 seconds", "89 seconds", "2 minutes", "60 minutes", "2 hours", "9 hours"])
        self.assertEqual(
            [ago(ms) for ms in [-5000, 59999, 60000, 3600000, 47 * 3600000, 48 * 3600000, None]],
            ["just now", "just now", "1 min ago", "1 hour ago", "47 hours ago", "2 days ago", ""],
        )

    def test_init_adds_its_own_entry_and_nothing_else(self):
        for target in TARGETS:
            first = merge_config(None, target["key"], target["entry"])
            self.assertEqual(first["status"], "new")
            self.assertEqual(json.loads(first["text"]), {target["key"]: {"since": target["entry"]}})
            self.assertEqual(merge_config(first["text"], target["key"], target["entry"])["status"], "same")
            self.assertEqual(target["entry"]["url"], MCP_URL)
        tabs = merge_config('{\n\t"mcpServers": {\n\t\t"other": { "command": "x" }\n\t}\n}\n', "mcpServers", TARGETS[0]["entry"])
        self.assertEqual(tabs["status"], "updated")
        self.assertIn('\n\t\t"other": {\n\t\t\t"command": "x"', tabs["text"])
        self.assertEqual(list(json.loads(tabs["text"])["mcpServers"]), ["other", "since"])
        reordered = '{"mcpServers":{"since":{"url":"https://api.since.dev/mcp","type":"http"}}}'
        self.assertEqual(merge_config(reordered, "mcpServers", TARGETS[0]["entry"])["status"], "same")
        for broken in ["[]", '{"mcpServers": []}', "{ // comment\n}", "not json", '{"a": NaN}']:
            self.assertEqual(merge_config(broken, "mcpServers", TARGETS[0]["entry"])["status"], "skipped", broken)

        added = merge_rule("# Agents\n\nBe kind.\n\n\n")
        self.assertEqual(added["text"], "# Agents\n\nBe kind.\n\n%s\n" % BLOCK)
        self.assertEqual(merge_rule(added["text"])["status"], "same")
        stale = added["text"].replace("## Since.dev", "## An older heading")
        self.assertEqual(merge_rule(stale)["text"], added["text"])

    def test_init_gives_each_coding_agent_its_own_rule(self):
        self.assertEqual([rule["file"] for rule in RULES], [".claude/rules/since.md", ".cursor/rules/since.mdc", "GEMINI.md", "AGENTS.md"])
        for text in [CLAUDE_RULE, CURSOR_RULE, BLOCK]:
            self.assertIn(RULE, text)
        self.assertRegex(CURSOR_RULE, r"^---\ndescription: .+\nalwaysApply: true\n---\n")
        self.assertEqual(merge_whole(None, CLAUDE_RULE), {"status": "new", "text": CLAUDE_RULE})
        self.assertEqual(merge_whole(CLAUDE_RULE, CLAUDE_RULE)["status"], "same")
        self.assertEqual(merge_whole("# Since.dev\n\nAn older rule.\n", CLAUDE_RULE), {"status": "updated", "text": CLAUDE_RULE})
        for yes in ["y", "Y", "yes", " Yes \n", "y\n"]:
            self.assertTrue(said_yes(yes), yes)
        for no in ["", "\n", "n", "no", "yep", "sure", None]:
            self.assertFalse(said_yes(no), str(no))

    def test_the_two_packages_carry_the_same_constants(self):
        """What is written twice, once per language, is the same twice."""
        script = read("src", "init.mjs")
        for paragraph in RULE.split("\n\n"):
            self.assertIn(json.dumps(paragraph, ensure_ascii=False), script)
        # The whole-file rules are built from the same header and words in both.
        for constant in [CLAUDE_RULE, CURSOR_RULE]:
            self.assertIn(json.dumps(constant.replace(RULE, "${RULE}"), ensure_ascii=False)[1:-1], script)
        local = read("src", "local.mjs")
        for pattern in PATTERNS:
            self.assertIn('"%s"' % pattern, local)
        self.assertEqual(re.search(r'VERSION = "([^"]+)"', read("src", "cli.mjs")).group(1), __version__)
        self.assertEqual(json.loads(read("package.json"))["version"], __version__)
        self.assertIn('version = "%s"' % __version__, read("pyproject.toml"))


if __name__ == "__main__":
    unittest.main()
