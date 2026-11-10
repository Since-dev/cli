// `since init`: the files that put Since in front of a coding agent, written into the repository so a clone carries them.
// It adds its own entry and nothing else: other servers stay as they are, and a file it cannot read as JSON is left alone.

export const MCP_URL = "https://api.since.dev/mcp"
export const TARGETS = [
  { file: ".mcp.json", key: "mcpServers", entry: { type: "http", url: MCP_URL }, what: "Claude Code" },
  { file: ".cursor/mcp.json", key: "mcpServers", entry: { url: MCP_URL }, what: "Cursor" },
  { file: ".vscode/mcp.json", key: "servers", entry: { type: "http", url: MCP_URL }, what: "VS Code" },
]
export const RULE_FILE = "AGENTS.md"
export const START = "<!-- since:start -->"
export const END = "<!-- since:end -->"
// The rule every listing ships (distribution/cursor/rules/since.mdc); a test holds the two to the same words.
export const RULE = [
  "When you change a dependency version, a manifest or lockfile, an AI model ID, a runtime version, a Docker base image or a CI runner label, check it with the Since.dev MCP tools first:",
  "",
  "- `check_code` with the manifest, lockfile, model ID or `surface` (`runtime:<product>@<version>`) reports what is affected, on which line, the days left and the replacement.",
  "- `whats_changed` with the package and `since` set to the current version lists every breaking change and deprecation between that version and today, each with its source.",
  "",
  "Treat `needs_review` and `unknown` as not settled, never as safe. Use the replacement the answer names. `fix_code` opens a fix pull request for a connected repository, and the editor asks before it runs.",
].join("\n")
export const BLOCK = `${START}\n## Since.dev\n\n${RULE}\n${END}`

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

const WORDS = {
  new: ["Wrote", "Would write"],
  updated: ["Updated", "Would update"],
  same: ["Unchanged", "Unchanged"],
  skipped: ["Skipped", "Skipped"],
}

export function init(io, program, dryRun) {
  const root = io.root()
  const lines = [""]
  const skipped = []
  let changed = 0
  const rows = TARGETS.map((target) => ({ ...target, result: mergeConfig(io.readFile(`${root}/${target.file}`), target.key, target.entry) }))
  rows.push({ file: RULE_FILE, what: "the rule every coding agent reads", result: mergeRule(io.readFile(`${root}/${RULE_FILE}`)) })
  for (const row of rows) {
    const { status, text } = row.result
    if (status === "new" || status === "updated") {
      changed += 1
      if (!dryRun) io.writeFile(`${root}/${row.file}`, text, null)
    }
    if (status === "skipped") skipped.push(row)
    lines.push(`  ${WORDS[status][dryRun ? 1 : 0].padEnd(14)}${row.file.padEnd(20)}${row.what}`)
  }
  lines.push("")
  for (const row of skipped) {
    lines.push(`  ${row.file} is not plain JSON, so it was left alone. Add this under "${row.key}" by hand:`)
    lines.push(`    "since": ${JSON.stringify(row.entry)}`)
    lines.push("")
  }
  if (dryRun) lines.push(changed ? `  Nothing was written. Run ${program} init to write ${changed === 1 ? "it" : "them"}.` : "  Everything is already set up.")
  else if (changed) {
    lines.push("  Commit these files: everyone who clones this repository gets the same setup.")
    lines.push("  The first tool call opens a sign-in in the browser. No key goes in any file.")
  } else lines.push("  Everything is already set up.")
  lines.push("")
  io.out(lines.join("\n"))
  return 0
}
