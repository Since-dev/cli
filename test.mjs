// node --test test.mjs
// Replays every fixture through the CLI with a fake network, folder and clock, and compares what it printed, what it
// requested and what it wrote with expected/<name>.txt. test_cli.py does the same with the Python package against the
// same files, which is what keeps the two printing the same text. UPDATE_EXPECTED=1 rewrites the expected files.
import assert from "node:assert/strict"
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { test } from "node:test"

import { VERSION, parseArgs, run } from "./src/cli.mjs"
import { BLOCK, CLAUDE_RULE, CURSOR_RULE, MCP_URL, RULE, RULES, TARGETS, mergeConfig, mergeRule, mergeWhole, saidYes } from "./src/init.mjs"
import { checkBody, parseRepo, patternRegExp } from "./src/local.mjs"
import { ago, wait } from "./src/render.mjs"

const here = (name) => new URL(name, import.meta.url)
const CWD = "/repo"
const HOME = "/home/dev"

/** JSON with sorted keys and no spaces: the form both languages can print alike. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`
  }
  return JSON.stringify(value)
}

export async function replay(fixture) {
  const files = new Map()
  for (const [name, content] of Object.entries(fixture.files ?? {})) files.set(name.startsWith("/") ? name : `${CWD}/${name}`, content)
  const out = []
  const err = []
  const log = []
  const wrote = []
  const answers = [...(fixture.http ?? [])]
  let now = Date.parse(fixture.now)
  const io = {
    runtime: "node",
    program: fixture.program ?? "since",
    env: fixture.env ?? {},
    cwd: CWD,
    home: HOME,
    platform: "linux",
    tty: Boolean(fixture.tty),
    out: (text) => void out.push(text),
    err: (text) => void err.push(text),
    now: () => now,
    sleep: async (ms) => {
      log.push(`sleep ${ms}`)
      now += ms
    },
    async fetch({ method, url, headers, body }) {
      assert.ok(url.startsWith("https://api.since.dev/"), url)
      assert.equal(headers["User-Agent"], `since-cli/${VERSION} (node)`)
      const address = url.slice("https://api.since.dev".length)
      log.push([`${method} ${address}`, headers.Authorization ? "key" : null, body === null ? null : canonical(JSON.parse(body))].filter(Boolean).join(" "))
      if (headers.Authorization) assert.equal(headers.Authorization, "Bearer fixture-key")
      const next = answers.shift()
      assert.ok(next, `no answer left for ${method} ${address}`)
      assert.equal(`${method} ${address.split("?")[0]}`, next.request)
      if (next.fail) throw new Error("network down")
      return { status: next.status, retryAfter: next.retryAfter ?? null, text: JSON.stringify(next.body) }
    },
    origin: () => fixture.origin ?? null,
    root: () => CWD,
    open: (url) => void log.push(`open ${url}`),
    prompt: async (question) => {
      log.push(`prompt ${question.trim()}`)
      return fixture.answer ?? ""
    },
    readFile: (file) => (files.has(file) ? files.get(file) : null),
    writeFile(file, text, mode) {
      files.set(file, text)
      wrote.push(`--- wrote ${file.startsWith(`${CWD}/`) ? file.slice(CWD.length + 1) : file}${mode === null ? "" : ` mode ${mode.toString(8)}`}\n${text}`)
    },
    remove(file) {
      if (!files.delete(file)) return false
      log.push(`remove ${file}`)
      return true
    },
    list(folder) {
      const entries = new Map()
      for (const file of files.keys()) {
        if (!file.startsWith(`${folder}/`)) continue
        const rest = file.slice(folder.length + 1).split("/")
        entries.set(rest[0], rest.length > 1)
      }
      return [...entries].map(([name, dir]) => ({ name, dir })).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
    },
  }
  const code = await run(fixture.args, io)
  assert.equal(answers.length, 0, "every prepared answer is asked for")
  let transcript = `$ ${["since", ...fixture.args].join(" ")}\n${out.join("")}`
  if (err.length) transcript += `--- stderr\n${err.join("")}`
  if (log.length) transcript += `--- log\n${log.join("\n")}\n`
  transcript += wrote.join("")
  return `${transcript}--- exit ${code}\n`
}

const names = readdirSync(here("./fixtures/")).filter((name) => name.endsWith(".json")).map((name) => name.slice(0, -5)).sort()
for (const name of names) {
  test(`fixture ${name}`, async () => {
    const fixture = JSON.parse(readFileSync(here(`./fixtures/${name}.json`), "utf8"))
    const transcript = await replay(fixture)
    const expected = here(`./expected/${name}.txt`)
    if (process.env.UPDATE_EXPECTED === "1") writeFileSync(expected, transcript)
    assert.ok(existsSync(expected), `expected/${name}.txt is missing: run UPDATE_EXPECTED=1 node --test test.mjs and read it`)
    assert.equal(transcript, readFileSync(expected, "utf8"))
  })
}

test("every expected file has its fixture", () => {
  const expected = readdirSync(here("./expected/")).map((name) => name.replace(/\.txt$/, "")).sort()
  assert.deepEqual(expected, names)
})

test("a repository is read from every form of name and remote", () => {
  const github = { host: "github.com", owner: "acme", name: "shop" }
  for (const form of [
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
  ]) assert.deepEqual(parseRepo(form), github, form)
  assert.deepEqual(parseRepo("git@gitlab.com:acme/shop.git"), { host: "gitlab.com", owner: "acme", name: "shop" })
  assert.deepEqual(parseRepo("https://github.com/acme/shop.js"), { host: "github.com", owner: "acme", name: "shop.js" })
  for (const form of ["", "shop", "../shop", "/srv/git/shop.git", "file:///srv/git/shop.git", "C:\\code\\shop", null]) assert.equal(parseRepo(form), null, String(form))
})

test("manifests match the patterns the Action uses, and a lockfile is sent as one", () => {
  assert.ok(patternRegExp("**/package.json").test("package.json"))
  assert.ok(patternRegExp("**/package.json").test("apps/web/package.json"))
  assert.ok(!patternRegExp("**/package.json").test("apps/web/package.json5"))
  assert.ok(patternRegExp("**/requirements*.txt").test("api/requirements-dev.txt"))
  assert.ok(patternRegExp("**/*.csproj").test("src/Api/Api.csproj"))
  assert.deepEqual(checkBody("api/go.mod", "module x"), { manifest: { path: "api/go.mod", content: "module x" }, detail: "summary" })
  assert.deepEqual(Object.keys(checkBody("api/uv.lock", "")), ["lockfile", "detail"])
  // go.sum is checksums, not versions to check: it goes as nothing special and is never among the files found.
  assert.deepEqual(Object.keys(checkBody("go.sum", "")), ["manifest", "detail"])
})

test("arguments: a switch, an option in both spellings, and what is refused", () => {
  assert.deepEqual(parseArgs(["check", "--fail-on=low", "--within", "30d", "--json"]).flags, { "fail-on": "low", within: "30d", json: true })
  assert.equal(parseArgs(["acme/shop", "--all"]).command, null)
  assert.equal(parseArgs(["--email"]).error, "--email needs a value.")
  assert.equal(parseArgs(["--verbose"]).error, "Unknown option: --verbose")
  assert.equal(parseArgs(["--all=yes"]).error, "Unknown option: --all=yes")
})

test("waits and ages read as words", () => {
  assert.deepEqual([1, 20, 89, 90, 3600, 5400, 32400].map(wait), ["1 second", "20 seconds", "89 seconds", "2 minutes", "60 minutes", "2 hours", "9 hours"])
  assert.deepEqual([-5000, 59_999, 60_000, 3_600_000, 47 * 3_600_000, 48 * 3_600_000, null].map(ago), ["just now", "just now", "1 min ago", "1 hour ago", "47 hours ago", "2 days ago", ""])
})

test("init adds its own entry and nothing else, and a second run changes nothing", () => {
  for (const target of TARGETS) {
    const first = mergeConfig(null, target.key, target.entry)
    assert.equal(first.status, "new")
    assert.deepEqual(JSON.parse(first.text), { [target.key]: { since: target.entry } })
    assert.equal(mergeConfig(first.text, target.key, target.entry).status, "same")
    assert.equal(target.entry.url, MCP_URL)
  }
  const tabs = mergeConfig('{\n\t"mcpServers": {\n\t\t"other": { "command": "x" }\n\t}\n}\n', "mcpServers", TARGETS[0].entry)
  assert.equal(tabs.status, "updated")
  assert.ok(tabs.text.includes('\n\t\t"other": {\n\t\t\t"command": "x"'))
  assert.deepEqual(Object.keys(JSON.parse(tabs.text).mcpServers), ["other", "since"])
  // The same entry with its fields in another order is still the entry.
  assert.equal(mergeConfig('{"mcpServers":{"since":{"url":"https://api.since.dev/mcp","type":"http"}}}', "mcpServers", TARGETS[0].entry).status, "same")
  for (const broken of ["[]", '{"mcpServers": []}', "{ // comment\n}", "not json"]) assert.equal(mergeConfig(broken, "mcpServers", TARGETS[0].entry).status, "skipped")

  const added = mergeRule("# Agents\n\nBe kind.\n\n\n")
  assert.equal(added.text, `# Agents\n\nBe kind.\n\n${BLOCK}\n`)
  assert.equal(mergeRule(added.text).status, "same")
  const stale = added.text.replace("## Since.dev", "## An older heading")
  assert.equal(mergeRule(stale).text, added.text)
  assert.equal(mergeRule(`${added.text}\nMore of the team's own rules.\n`).status, "same")
})

test("init gives each coding agent its own rule, the same words in each, and writes nothing without a yes", () => {
  // One file per agent: Claude Code reads no AGENTS.md when a repository has a CLAUDE.md, so it gets a rule of its own.
  assert.deepEqual(RULES.map((rule) => rule.file), [".claude/rules/since.md", ".cursor/rules/since.mdc", "GEMINI.md", "AGENTS.md"])
  for (const text of [CLAUDE_RULE, CURSOR_RULE, BLOCK]) assert.ok(text.includes(RULE))
  // Cursor's applies in every chat, with no list of files to wait for.
  assert.match(CURSOR_RULE, /^---\ndescription: .+\nalwaysApply: true\n---\n/)
  // The rule says what Since checks, when to call it (before the write, never after the task), which tool, and when not to; it never
  // orders a check on everything.
  assert.match(RULE, /^Since\.dev \(the `since` MCP server\) checks AI model ids/)
  assert.match(RULE, /\n\nCall it before you write an AI model id, add or upgrade a dependency/)
  assert.match(RULE, /\n\nWhich tool:\n- whats_changed: .+\n- check_code: .+\n- fix_code: /)
  assert.match(RULE, /\n\nSkip it for code that uses no package, model, API or runtime, [^\n]+\.$/)
  assert.doesNotMatch(RULE, /before you finish/)
  assert.doesNotMatch(RULE, /\b(always|must|every task|MUST|CRITICAL)\b/)

  assert.deepEqual(mergeWhole(null, CLAUDE_RULE), { status: "new", text: CLAUDE_RULE })
  assert.equal(mergeWhole(CLAUDE_RULE, CLAUDE_RULE).status, "same")
  assert.deepEqual(mergeWhole("# Since.dev\n\nAn older rule.\n", CLAUDE_RULE), { status: "updated", text: CLAUDE_RULE })

  for (const yes of ["y", "Y", "yes", " Yes \n", "y\n"]) assert.equal(saidYes(yes), true, yes)
  for (const no of ["", "\n", "n", "no", "yep", "sure", null, undefined]) assert.equal(saidYes(no), false, String(no))
})
