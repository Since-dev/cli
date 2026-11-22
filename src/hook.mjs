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
export function nearestDeclarations(io, cwd, path) {
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

// ── The session brief (SessionStart) ────────────────────────────────────────────────────────────────────────────────────────────────

/** How long the brief waits for Since: once a day a session starts a few seconds later; past it, the session starts with no brief. */
export const BRIEF_TIMEOUT_MS = 15_000
/** A brief is asked for again after a day, or sooner when the files it read changed. */
const BRIEF_DAY_MS = 86_400_000
/** How many entries the brief's walk of the project looks at, and how many files that name a model it sends. */
const BRIEF_WALK_ENTRIES = 20_000
const BRIEF_MODEL_FILES = 20
const BRIEF_FILE_CHARACTERS = 200_000
const RUNTIME_FILE = /^(?:\.nvmrc|\.node-version|\.python-version|\.tool-versions|global\.json|Dockerfile[^/]*|(?:docker-)?compose[^/]*\.ya?ml)$/i
const WORKFLOW = /^\.github\/workflows\/[^/]+\.ya?ml$/i
const CODE = /\.(?:[cm]?[jt]sx?|py|go|rb|java|kt|cs|php|rs|swift|scala|ex|exs|dart)$/i
const MODEL_ID = /["'`](?:gpt-|o[134](?:-|["'`])|chatgpt-|claude-|gemini-|text-embedding-|dall-e-|whisper-|mistral-|codestral-|command-r|deepseek-)/i

/**
 * What the brief sends, as one set within the API's limits: the manifests nearest the top first, the runtime and CI files, up to
 * `BRIEF_MODEL_FILES` code files that name a model id, then the lockfiles beside the manifests while they fit.
 */
export function briefFiles(io, cwd) {
  const found = { manifests: [], lockfiles: [], runtimes: [], code: [] }
  let seen = 0
  const walk = (folder) => {
    for (const entry of io.list(folder ? `${cwd}/${folder}` : cwd)) {
      if (++seen > BRIEF_WALK_ENTRIES) return
      const path = folder ? `${folder}/${entry.name}` : entry.name
      if (entry.dir) {
        if (!SKIPPED.has(entry.name) && (!entry.name.startsWith(".") || entry.name === ".github" || path === ".github/workflows")) walk(path)
      } else if (MANIFEST.test(entry.name)) found.manifests.push(path)
      else if (LOCKFILE.test(entry.name)) found.lockfiles.push(path)
      else if (RUNTIME_FILE.test(entry.name) || WORKFLOW.test(path)) found.runtimes.push(path)
      else if (CODE.test(entry.name)) found.code.push(path)
    }
  }
  walk("")
  const depth = (path) => path.split("/").length
  const byDepth = (a, b) => depth(a) - depth(b) || (a < b ? -1 : a > b ? 1 : 0)
  const out = []
  let size = 0
  const add = (path, test = () => true) => {
    if (out.length >= MAX_FILES || out.some((file) => file.path === path)) return false
    const content = io.readFile(`${cwd}/${path}`)
    if (content === null || !content.trim() || content.length > (LOCKFILE.test(path.split("/").pop()) ? MAX_CHARACTERS : BRIEF_FILE_CHARACTERS)
      || size + content.length > MAX_CHARACTERS || !test(content)) return false
    out.push({ path, content })
    size += content.length
    return true
  }
  for (const path of found.manifests.sort(byDepth)) add(path)
  for (const path of found.runtimes.sort(byDepth)) add(path)
  let models = 0
  for (const path of found.code.sort(byDepth)) if (models < BRIEF_MODEL_FILES && add(path, (content) => MODEL_ID.test(content))) models++
  const manifestFolders = new Set(out.filter((file) => MANIFEST.test(file.path.split("/").pop())).map((file) => file.path.split("/").slice(0, -1).join("/")))
  for (const path of found.lockfiles.sort(byDepth)) if (manifestFolders.has(path.split("/").slice(0, -1).join("/"))) add(path)
  return out
}

const major = (version) => Number.parseInt(String(version ?? "").replace(/^[^0-9]*/, ""), 10)

/** One place, as file:line. */
const placeOf = (where) => {
  const place = Array.isArray(where) ? where.find((item) => item && item.file) : null
  return place ? `${place.file}${typeof place.line === "number" ? `:${place.line}` : ""}` : null
}

/** One dated finding in a line: what, where, the date and how far, and what to move to. */
function datedLine(finding) {
  const name = [finding.surface?.name, finding.surface?.kind === "model" ? null : finding.version].filter(Boolean).join(" ")
  const when = typeof finding.daysLeft !== "number" ? "" : finding.daysLeft < 0 ? ", passed" : finding.daysLeft === 0 ? ", today" : `, in ${finding.daysLeft} ${finding.daysLeft === 1 ? "day" : "days"}`
  const date = finding.effectiveDate ? `${finding.effectiveDate}${when}` : String(finding.reason ?? "").trim().split(/(?<=\.)\s/)[0]
  const where = placeOf(finding.where)
  return `${name}${where ? ` (${where})` : ""}: ${date}${finding.replacement ? `; use ${finding.replacement}` : ""}`
}

/** The brief's card from a check of files: runtimes and models with their dates, packages a major release behind, and the totals. */
export function briefCard(answer) {
  const findings = Array.isArray(answer.findings) ? answer.findings : []
  const affected = findings.filter((finding) => finding && finding.state === "affected")
  const of = (kinds) => affected.filter((finding) => kinds.includes(finding.surface?.kind))
  const dedupe = (lines) => [...new Set(lines)]
  const runtimes = dedupe(of(["runtime", "image"]).map(datedLine)).slice(0, 5)
  const models = dedupe(of(["model"]).map(datedLine)).slice(0, 5)
  const behind = (Array.isArray(answer.checked) ? answer.checked : [])
    .filter((item) => item?.surface?.kind === "package" && item.version && item.latest && major(item.latest) > major(item.version))
    .sort((a, b) => (major(b.latest) - major(b.version)) - (major(a.latest) - major(a.version)))
    .slice(0, 5).map((item) => `${item.surface.name} ${item.version} (latest ${item.latest})`)
  const asOf = typeof answer.coverage?.asOf === "string" ? answer.coverage.asOf.slice(0, 10) : null
  const lines = [`Since.dev brief for this project${asOf ? `, as of ${asOf}` : ""} (its manifests, runtime files and the files that name a model):`]
  if (runtimes.length) lines.push(`- Runtimes and images: ${runtimes.join("; ")}.`)
  if (models.length) lines.push(`- Models: ${models.join("; ")}.`)
  if (behind.length) lines.push(`- A major release behind: ${behind.join(", ")}.`)
  const critical = affected.filter((finding) => finding.severity === "critical").length
  const review = findings.filter((finding) => finding?.state === "needs_review").length
  if (affected.length) lines.push(`- In all, ${affected.length} ${affected.length === 1 ? "finding affects" : "findings affect"} this code${critical ? ` (${critical} critical)` : ""}${review ? `, and ${review} to review` : ""}. Check before you write against any of these.`)
  else lines.push(`- Nothing it names is affected by a known change${review ? ` (${review} to review)` : ""}.`)
  return lines.join("\n")
}

/**
 * The SessionStart hook: a short card of what the project runs on and what ended or is ending there, from one check of its files a
 * day (sooner when those files changed); between checks the card is said again from the cache. Returns the hook's JSON output, or
 * null. `cache` is the stored brief for this project, updated in place.
 */
export async function sessionStart(ctx, event, cache, check) {
  const cwd = event && typeof event.cwd === "string" ? event.cwd.replace(/\/+$/, "") : null
  if (!cwd) return null
  const files = briefFiles(ctx.io, cwd)
  if (!files.length) return null
  const seen = hash(JSON.stringify(files.map((file) => [file.path, hash(file.content)])))
  const now = ctx.io.now()
  const say = (card) => ({ hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: card } })
  if (cache.seen === seen && typeof cache.card === "string" && now - (cache.at ?? 0) < BRIEF_DAY_MS) return say(cache.card)
  // The full answer: a summary counts what was checked, and the brief needs each package's version and latest.
  const answer = await check({ files, detail: "full" })
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.findings)) return null
  const card = briefCard(answer.json)
  Object.assign(cache, { seen, at: now, card })
  return say(card)
}
