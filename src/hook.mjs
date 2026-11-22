// The write-time hook (Feature 5): Claude Code runs `since hook post-tool-use` after each file its agent writes. It checks the file as
// it now stands, as one set of files, and tells the agent only what is proven on the lines it just wrote, in a short note. A file that
// names no model, version, runtime or package is never sent, nor one unchanged since its last check, and the same note is never said
// twice in a session. It answers within six seconds or stays silent: a slow or unreachable Since never holds the agent up longer.

import { createHash } from "node:crypto"

/**
 * How long the hook waits for Since: past it, nothing is said and the agent goes on (the finish check says it later). A model id just
 * written is read closely by a model before Since answers: about 3 seconds in all in production (Nov 22, 2026), against 0.4 to 1.2 s
 * for any other file, so the wait is six seconds.
 */
export const HOOK_TIMEOUT_MS = 6000
/** The largest file the hook sends: the API's own limit on a set of files. */
const MAX_CHARACTERS = 500_000

// What makes a file worth a check: an import or require, a model id, a runtime or image line, or a manifest or lockfile by its name.
const NAMES_SOMETHING = /\b(?:import|require|from|using|use)\b|\b(?:gpt-|o[134]-|claude-|gemini-|mistral|llama|command-r|text-embedding|dall-e|whisper|deepseek)|^\s*FROM\s|\b(?:runs-on|uses|node-version|python-version|go-version|image)\s*:/im
const DECLARES = /(?:^|\/)(?:package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json|pom\.xml|build\.gradle(?:\.kts)?|[^/]+\.csproj|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose[^/]*\.ya?ml|\.nvmrc|\.python-version|\.tool-versions)$/i

/** Whether a file is worth sending: one that names a model, version, runtime or package. */
export function namesSomething(path, content) {
  return DECLARES.test(path) || NAMES_SOMETHING.test(content)
}

/** The 1-based lines `piece` occupies in `content`, every occurrence when `all`. */
function linesOf(content, piece, all) {
  const lines = new Set()
  if (!piece) return lines
  let from = 0
  for (;;) {
    const at = content.indexOf(piece, from)
    if (at < 0) break
    const first = content.slice(0, at).split("\n").length
    const count = piece.split("\n").length
    for (let line = first; line < first + count; line++) lines.add(line)
    if (!all) break
    from = at + Math.max(1, piece.length)
  }
  return lines
}

/** What the agent just wrote: the file's path inside the project, its text now, and the lines written (null for every line). */
export function written(event, readFile) {
  const input = event && typeof event.tool_input === "object" && event.tool_input ? event.tool_input : {}
  const file = typeof input.file_path === "string" ? input.file_path : null
  const cwd = typeof event?.cwd === "string" ? event.cwd.replace(/\/+$/, "") : null
  if (!file || !cwd) return null
  const path = file.startsWith(`${cwd}/`) ? file.slice(cwd.length + 1) : file.startsWith("/") ? null : file
  if (!path || path.split("/").some((part) => part === ".." || part === ".git" || part === "node_modules")) return null
  if (event.tool_name === "Write" && typeof input.content === "string") return { path, content: input.content, lines: null }
  const content = readFile(file.startsWith("/") ? file : `${cwd}/${file}`)
  if (content === null) return null
  const pieces = Array.isArray(input.edits) ? input.edits.map((edit) => ({ text: edit?.new_string, all: Boolean(edit?.replace_all) }))
    : [{ text: input.new_string, all: Boolean(input.replace_all) }]
  const lines = new Set()
  for (const piece of pieces) if (typeof piece.text === "string") for (const line of linesOf(content, piece.text, piece.all)) lines.add(line)
  return { path, content, lines }
}

/** One note a line: the place, what ended or changed, and what to write when the answer gives it. */
function noteOf(finding, place) {
  const reason = String(finding.reason ?? "").trim().replace(/\.?$/, ".")
  const replacement = finding.replacement && !reason.toLowerCase().includes(String(finding.replacement).toLowerCase()) ? ` Use ${finding.replacement}.` : ""
  const edit = finding.edit && typeof finding.edit.to === "string" && finding.edit.to.trim() ? ` Write: ${finding.edit.to.trim()}` : ""
  return `${place.file}:${place.line}: ${reason}${replacement}${edit}`
}

const hash = (text) => createHash("sha256").update(text).digest("hex").slice(0, 32)

/**
 * The PostToolUse hook. `state` is the session's memory (each file's last checked text and the notes said), read and written by the
 * caller. Returns the note for the agent, or null to say nothing.
 */
export async function postToolUse(ctx, event, state, check) {
  const wrote = written(event, (file) => ctx.io.readFile(file))
  if (!wrote || !wrote.content.trim() || wrote.content.length > MAX_CHARACTERS || !namesSomething(wrote.path, wrote.content)) return null
  const seen = hash(wrote.content)
  if (state.files[wrote.path] === seen) return null
  // Every file written that names something, answered or not: the finish check reads them all.
  state.written = [...new Set([...(state.written ?? []), wrote.path])]
  const answer = await check({ files: [{ path: wrote.path, content: wrote.content }], detail: "summary" })
  // Slow, unreachable or refused: silent, and the file is checked again at its next write or at the finish check.
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.findings)) return null
  state.files[wrote.path] = seen
  const notes = []
  for (const finding of answer.json.findings) {
    if (finding.state !== "affected") continue
    for (const place of Array.isArray(finding.where) ? finding.where : []) {
      if (place?.file !== wrote.path || typeof place.line !== "number") continue
      if (wrote.lines && !wrote.lines.has(place.line)) continue
      const key = `${finding.changeId ?? finding.reason}\u0000${place.file}\u0000${place.line}`
      if (state.notes.includes(key)) continue
      state.notes.push(key)
      notes.push(noteOf(finding, place))
    }
  }
  return notes.length ? `Since.dev checked what was just written and found:\n${notes.map((note) => `- ${note}`).join("\n")}` : null
}

// ── The finish check (Stop) ─────────────────────────────────────────────────────────────────────────────────────────────────────────

/** How long the finish check waits for Since: the agent has finished its turn, so a few seconds more costs nobody a step. */
export const STOP_TIMEOUT_MS = 10_000
/** The most files one check of files holds: the API's own limit. */
const MAX_FILES = 50
const MANIFEST = /^(?:package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json|pom\.xml|build\.gradle(?:\.kts)?|[^/]+\.csproj)$/i
const LOCKFILE = /^(?:package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lock|poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|packages\.lock\.json|gradle\.lockfile)$/
const SKIPPED = new Set([".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "target", ".next", "__pycache__"])

/** Whether a path inside the project is one the finish check reads: never in a dependency or build folder. */
const readable = (path) => !path.split("/").some((part) => part === ".." || SKIPPED.has(part))

/** The nearest manifest at or above `path`'s folder and the lockfiles beside it, so imports are read at the versions the project installs. */
function nearestDeclarations(io, cwd, path) {
  const parts = path.split("/").slice(0, -1)
  for (let depth = parts.length; depth >= 0; depth--) {
    const folder = parts.slice(0, depth).join("/")
    const names = io.list(folder ? `${cwd}/${folder}` : cwd).filter((entry) => !entry.dir).map((entry) => entry.name).sort()
    const manifests = names.filter((name) => MANIFEST.test(name))
    if (manifests.length) return [...manifests, ...names.filter((name) => LOCKFILE.test(name))].map((name) => (folder ? `${folder}/${name}` : name))
  }
  return []
}

/**
 * What the finish check sends: every file the session wrote or git shows changed that names something, then the manifests and lockfiles
 * nearest each, as one set within the API's limits. `changed` are paths inside the project.
 */
export function finishFiles(io, cwd, written, changed) {
  const paths = [...new Set([...written, ...changed])].filter(readable)
  const out = []
  const seen = new Set()
  let size = 0
  const add = (path, always) => {
    if (seen.has(path) || out.length >= MAX_FILES) return
    seen.add(path)
    const content = io.readFile(`${cwd}/${path}`)
    if (content === null || !content.trim() || size + content.length > MAX_CHARACTERS || (!always && !namesSomething(path, content))) return
    out.push({ path, content })
    size += content.length
  }
  for (const path of paths) add(path, false)
  const sent = out.map((file) => file.path)
  for (const path of sent) for (const declaration of nearestDeclarations(io, cwd, path)) add(declaration, true)
  return { files: out, changed: sent }
}

/**
 * The Stop hook: when the agent finishes its turn, the whole change is checked as one set of files, and the agent is told what is proven
 * in the files it changed that it has not been told already, with what was covered. Nothing when the change is unchanged since the last
 * finish check, clean, or Since is slow. `block` (the team's choice) makes the agent go on to fix it; otherwise the note waits for it.
 * Returns the hook's JSON output, or null.
 */
export async function stopHook(ctx, event, state, check, changed, { block = false } = {}) {
  // Claude Code is already going on because of a Stop hook: never a second round.
  if (!event || event.stop_hook_active === true) return null
  const cwd = typeof event.cwd === "string" ? event.cwd.replace(/\/+$/, "") : null
  if (!cwd) return null
  const { files, changed: sent } = finishFiles(ctx.io, cwd, [...(state.written ?? []), ...Object.keys(state.files ?? {})], changed)
  if (!sent.length) return null
  const seen = hash(JSON.stringify(files.map((file) => [file.path, hash(file.content)])))
  if (state.finished === seen) return null
  const answer = await check({ files, detail: "summary" })
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.findings)) return null
  state.finished = seen
  const ours = new Set(sent)
  const notes = []
  for (const finding of answer.json.findings) {
    if (finding.state !== "affected") continue
    for (const place of Array.isArray(finding.where) ? finding.where : []) {
      if (!ours.has(place?.file) || typeof place.line !== "number") continue
      const key = `${finding.changeId ?? finding.reason}\u0000${place.file}\u0000${place.line}`
      if (state.notes.includes(key)) continue
      state.notes.push(key)
      notes.push(noteOf(finding, place))
    }
  }
  if (!notes.length) return null
  const one = sent.length === 1
  const covered = `${one ? "the file" : `the ${sent.length} files`} changed in this session${files.length > sent.length ? ` with the manifests and lockfiles beside ${one ? "it" : "them"}` : ""}`
  const note = `Since.dev checked ${covered}, and found:\n${notes.map((line) => `- ${line}`).join("\n")}`
  return block ? { decision: "block", reason: note } : { hookSpecificOutput: { hookEventName: "Stop", additionalContext: note } }
}
