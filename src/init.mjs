// `since init`: the files that put Since in front of a coding agent, written into the repository so a clone carries them.
// It adds its own entry and nothing else: other servers stay as they are, and a file it cannot read as JSON is left alone.
// Two kinds of file: the MCP server's entry for each editor, and a rule for each coding agent, which is what gets a check made
// without being asked for. Each agent reads its own file (Claude Code reads no AGENTS.md when a repository has a CLAUDE.md), so
// each gets one. Nothing is written until the person says yes: it shows what it would write, then asks.

export const MCP_URL = "https://api.since.dev/mcp"
export const TARGETS = [
  { file: ".mcp.json", key: "mcpServers", entry: { type: "http", url: MCP_URL }, what: "Claude Code" },
  { file: ".cursor/mcp.json", key: "mcpServers", entry: { url: MCP_URL }, what: "Cursor" },
  { file: ".vscode/mcp.json", key: "servers", entry: { type: "http", url: MCP_URL }, what: "VS Code" },
]
export const START = "<!-- since:start -->"
export const END = "<!-- since:end -->"
// The rule, a paragraph to a string: what Since checks, when to call it, which tool, and what to skip. The same words wherever a coding
// agent reads them: here, in the rule every listing ships (distribution/cursor/rules/since.mdc), in the Python copy, and in what the
// server says on connect. Tests hold them to the same words.
export const RULE = [
  "Since.dev (the `since` MCP server) checks AI model ids, dependency upgrades, Dockerfiles, GitHub Actions workflows and runtime versions for breaking changes, deprecations and retirements published after your training.",
  "Call it before you write an AI model id, add or upgrade a dependency, write or edit a Dockerfile, a compose file or a CI workflow (the images, runners, actions and runtime versions they name), set a runtime version (.nvmrc, engines, go.mod, pyproject.toml), or write code that calls an AI model or a fast-changing SDK or API; and when an error follows a version bump or began with no code change. Your training data may not include these changes, even for well-known packages such as react, next, openai or pydantic. Call it even when the version was given to you or the fix seems well known: do what was asked, and report what it finds.",
  "Which tool:\n- whats_changed: what changed in a package, model, provider, API or runtime since a version, a date or your training cutoff. With provider and current=true, the model ids to use in new code.\n- check_code: what in this code, file, model id or version is affected, on which line, and what to move to. A runtime version from any file is surface (runtime:nodejs@18); a Dockerfile, a compose file or a GitHub Actions workflow is code with its path.\n- fix_code: the fix, for a snippet or a connected repository. It runs the check itself.",
  "Skip it for code that uses no package, model, API or runtime, for general programming questions, and for the repository's own git history.",
].join("\n\n")
export const BLOCK = `${START}\n## Since.dev\n\n${RULE}\n${END}`
// A rule file of its own is the whole file. Cursor's carries the header that makes it apply in every chat.
export const CLAUDE_RULE = `# Since.dev\n\n${RULE}\n`
export const CURSOR_RULE = `---\ndescription: Check AI model ids, dependencies, Dockerfiles, CI files and runtime versions with Since.dev\nalwaysApply: true\n---\n\n${RULE}\n`
// Each coding agent's rule: a file of Since's own where the agent reads a folder of rules, a marked block in the file it shares with the team's own.
export const RULES = [
  { file: ".claude/rules/since.md", what: "Claude Code's rule", whole: CLAUDE_RULE },
  { file: ".cursor/rules/since.mdc", what: "Cursor's rule", whole: CURSOR_RULE },
  { file: "GEMINI.md", what: "Gemini CLI's rule" },
  { file: "AGENTS.md", what: "the rule Codex and other agents read" },
]

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value)

function sameEntry(have, want) {
  if (!isObject(have)) return false
  const keys = Object.keys(want)
  return Object.keys(have).length === keys.length && keys.every((key) => have[key] === want[key])
}

/** The indent a JSON file already uses: its first indented line decides, two spaces when there is none. */
function indentOf(text) {
  const match = /^([ \t]+)\S/m.exec(text)
  if (!match) return 2
  return match[1].includes("\t") ? "\t" : match[1].length
}

/** A config file with the Since entry in it. status: new, updated, same, or skipped when the file is not plain JSON. */
export function mergeConfig(text, key, entry) {
  if (text === null || text.trim() === "") return { status: "new", text: `${JSON.stringify({ [key]: { since: entry } }, null, 2)}\n` }
  let data
  try {
    data = JSON.parse(text)
  } catch {
    return { status: "skipped", text }
  }
  if (!isObject(data) || (data[key] !== undefined && !isObject(data[key]))) return { status: "skipped", text }
  if (data[key] !== undefined && sameEntry(data[key].since, entry)) return { status: "same", text }
  if (data[key] === undefined) data[key] = {}
  data[key].since = entry
  return { status: "updated", text: `${JSON.stringify(data, null, indentOf(text))}\n` }
}

/** AGENTS.md with the rule between its two markers: replaced when the markers are there, added at the end when they are not. */
export function mergeRule(text) {
  if (text === null || text.trim() === "") return { status: "new", text: `${BLOCK}\n` }
  const start = text.indexOf(START)
  const end = start < 0 ? -1 : text.indexOf(END, start)
  if (start < 0 || end < 0) return { status: "updated", text: `${text.replace(/\n+$/, "")}\n\n${BLOCK}\n` }
  const next = text.slice(0, start) + BLOCK + text.slice(end + END.length)
  return next === text ? { status: "same", text } : { status: "updated", text: next }
}

/** A file that is Since's own, whole: written when it is missing, replaced when it holds an older rule. */
export function mergeWhole(text, whole) {
  if (text === null || text.trim() === "") return { status: "new", text: whole }
  return text === whole ? { status: "same", text } : { status: "updated", text: whole }
}

/** Whether an answer to a yes-or-no question is a yes. Anything else, an empty line included, is a no. */
export const saidYes = (answer) => /^\s*y(es)?\s*$/i.test(String(answer ?? ""))

const WORDS = {
  new: ["Wrote", "Would write"],
  updated: ["Updated", "Would update"],
  same: ["Unchanged", "Unchanged"],
  skipped: ["Skipped", "Skipped"],
}

export async function init(io, program, { dryRun = false, yes = false } = {}) {
  const root = io.root()
  const rows = TARGETS.map((target) => ({ ...target, result: mergeConfig(io.readFile(`${root}/${target.file}`), target.key, target.entry) }))
  for (const rule of RULES) {
    const text = io.readFile(`${root}/${rule.file}`)
    rows.push({ ...rule, result: rule.whole ? mergeWhole(text, rule.whole) : mergeRule(text) })
  }
  const changes = rows.filter((row) => row.result.status === "new" || row.result.status === "updated")
  const skipped = rows.filter((row) => row.result.status === "skipped")
  const width = Math.max(...rows.map((row) => row.file.length)) + 2
  /** The list of files, as what would happen or as what did. */
  const listing = (wrote) => ["", ...rows.map((row) => `  ${WORDS[row.result.status][wrote ? 0 : 1].padEnd(14)}${row.file.padEnd(width)}${row.what}`), ""]
  const byHand = skipped.flatMap((row) => [`  ${row.file} is not plain JSON, so it was left alone. Add this under "${row.key}" by hand:`, `    "since": ${JSON.stringify(row.entry)}`, ""])
  const them = changes.length === 1 ? "it" : "them"
  const say = (lines) => io.out(`${lines.join("\n")}\n`)
  const next = ["  Commit these files: everyone who clones this repository gets the same setup.", "  The first tool call opens a sign-in in the browser. No key goes in any file."]

  if (!changes.length) {
    say([...listing(true), ...byHand, "  Everything is already set up."])
    return 0
  }
  // Nothing is written before a yes: --yes on the command line, or the answer to the question. With nobody to ask, it says how.
  if (yes && !dryRun) {
    for (const row of changes) io.writeFile(`${root}/${row.file}`, row.result.text, null)
    say([...listing(true), ...byHand, ...next])
    return 0
  }
  say([...listing(false), ...byHand])
  if (dryRun) return say([`  Nothing was written. Run ${program} init to write ${them}.`]), 0
  if (!io.tty) return say([`  Nothing was written: there is nobody to ask here. Run ${program} init --yes to write ${them}.`]), 0
  if (!saidYes(await io.prompt(`  Write ${changes.length === 1 ? "this file" : `these ${changes.length} files`}? [y/N] `))) return say(["  Nothing was written."]), 0
  for (const row of changes) io.writeFile(`${root}/${row.file}`, row.result.text, null)
  say(["", `  Wrote ${changes.length === 1 ? "it" : `${changes.length} files`}.`, ...next])
  return 0
}
