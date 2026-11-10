// The whole CLI: run(argv, io) returns the exit code. Everything it touches (network, files, clock, terminal) comes in
// through io, so the tests replay it from fixtures, and since_dev/cli.py prints the same text from the same fixtures.
//
// Exit codes: 0 done. `check`: 1 when a finding fails, 2 when the caller has something to fix, 0 when Since could not be
// reached (an outage never fails a build). Every other command: 1 when it could not finish, 2 for a usage error.

import { init } from "./init.mjs"
import { MAX_CHARACTERS, checkBody, findManifests, parseRepo } from "./local.mjs"
import * as text from "./render.mjs"

export const VERSION = "0.1.3"
const DEFAULT_API = "https://api.since.dev"
const SITE = "https://since.dev"
const APP = "https://app.since.dev"
const COMMANDS = ["check", "calendar", "changed", "init", "login", "logout", "help", "version"]
const SWITCHES = ["all", "json", "local", "dry-run", "no-color", "help", "version"]
const OPTIONS = ["fail-on", "within", "provider", "ecosystem", "since", "email", "max-files"]
const FAIL_ON = ["low", "medium", "high", "critical", "none"]
const MAX_PAGES = 10
const MAX_WAIT_MS = 60_000

export function parseArgs(argv) {
  const args = { command: null, positional: [], flags: {}, error: null }
  for (let index = 0; index < argv.length; index++) {
    const word = argv[index]
    if (word === "-h") args.flags.help = true
    else if (word === "-v") args.flags.version = true
    else if (word.startsWith("--")) {
      const equals = word.indexOf("=")
      const name = equals < 0 ? word.slice(2) : word.slice(2, equals)
      if (SWITCHES.includes(name) && equals < 0) args.flags[name] = true
      else if (OPTIONS.includes(name)) {
        const value = equals < 0 ? argv[++index] : word.slice(equals + 1)
        if (value === undefined || value === "") {
          args.error = `--${name} needs a value.`
          return args
        }
        args.flags[name] = value
      } else {
        args.error = `Unknown option: ${word}`
        return args
      }
    } else if (args.command === null && args.positional.length === 0 && COMMANDS.includes(word)) args.command = word
    else args.positional.push(word)
  }
  return args
}

function help(p) {
  const usage = [
    ["", "check this repository (reads the origin remote)"],
    [" <owner/repo>", "check a public GitHub repository"],
    [" check", "the same for CI: exit 1 when a finding fails"],
    [" calendar", "what ends in the next 30 days"],
    [" changed <package>", "breaking changes and deprecations of a package"],
    [" init", "set up your coding agent to check before it writes"],
    [" login", "sign in, for private repositories"],
    [" logout", "remove the stored key"],
  ]
  const options = [
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
  const width = Math.max(...usage.map(([words]) => p.length + words.length)) + 3
  const lines = [`since-dev ${VERSION}: what in this repository is about to stop working`, "", "Usage"]
  for (const [words, what] of usage) lines.push(`  ${(p + words).padEnd(width)}${what}`)
  lines.push("", "Options")
  for (const [words, what] of options) lines.push(`  ${words.padEnd(21)}${what}`)
  lines.push("", `Docs  ${SITE}/docs#cli`, "")
  return lines.join("\n")
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value)
const clamp = (value, low, high) => Math.min(high, Math.max(low, value))
const wholeNumber = (value) => (typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : null)

function readJson(content) {
  if (content === null) return null
  try {
    const data = JSON.parse(content)
    return isObject(data) ? data : null
  } catch {
    return null
  }
}

function credentialsPath(io) {
  if (io.platform === "win32" && io.env.APPDATA) return `${io.env.APPDATA}\\since\\credentials.json`
  return `${io.env.XDG_CONFIG_HOME || `${io.home}/.config`}/since/credentials.json`
}

/** The key to call with: SINCE_DEV_KEY, or the one `login` stored. A stored key only ever goes to the API it came from. */
function readKey(io, api) {
  const fromEnv = String(io.env.SINCE_DEV_KEY ?? "").trim()
  if (fromEnv) return { value: fromEnv, source: "env" }
  const stored = readJson(io.readFile(credentialsPath(io)))
  if (stored && typeof stored.api_key === "string" && stored.api_key && (stored.api || DEFAULT_API) === api) {
    return { value: stored.api_key, source: "file" }
  }
  return null
}

function encodeQuery(query) {
  return Object.entries(query)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([name, value]) => `${name}=${encodeURIComponent(String(value))}`)
    .join("&")
}

/** One request. status 0 means Since could not be reached; json is the answer when it is a JSON object. */
async function call(ctx, method, path, { query, body, key } = {}) {
  const search = query ? encodeQuery(query) : ""
  const headers = { "User-Agent": `since-cli/${VERSION} (${ctx.io.runtime})`, Accept: "application/json" }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  if (key) headers.Authorization = `Bearer ${key.value}`
  let answer
  try {
    answer = await ctx.io.fetch({
      method,
      url: `${ctx.api}${path}${search ? `?${search}` : ""}`,
      headers,
      body: body === undefined ? null : JSON.stringify(body),
    })
  } catch {
    return { status: 0, json: null, retryAfter: null }
  }
  const retryAfter = Number.parseInt(String(answer.retryAfter ?? ""), 10)
  return { status: answer.status, json: readJson(answer.text ?? null), retryAfter: Number.isFinite(retryAfter) ? retryAfter : null }
}

const errorOf = (answer) => (answer.json && typeof answer.json.error === "string" ? answer.json.error : null)

/** Say why a request did not answer, and return the exit code that goes with it. */
function refuse(ctx, answer) {
  const { io, p, check } = ctx
  const message = answer.json && typeof answer.json.message === "string" ? answer.json.message : null
  if (answer.status === 0 || answer.status >= 500) {
    const what = errorOf(answer) === "busy" ? "Since is still busy." : "Since could not be reached."
    io.err(`${what} Try again in a minute.${check ? " Nothing was checked, so this run passes." : ""}\n`)
    return check ? 0 : 1
  }
  if (answer.status === 401) {
    io.err(
      ctx.key && ctx.key.source === "env"
        ? `Since refused the key in SINCE_DEV_KEY. Create a new one at ${APP}/api-keys\n`
        : `Since refused the stored key. Sign in again: ${p} login\n`,
    )
  } else if (answer.status === 429) {
    const seconds = wholeNumber(answer.json ? answer.json.retryAfterSec : null) ?? answer.retryAfter
    // A shared CI address runs out of free scans through no fault of the build, so a limit passes a check like an outage does.
    io.err(`${message ?? "Too many requests."}${seconds ? ` Try again in ${text.wait(seconds)}.` : ""}${check ? " Nothing was checked, so this run passes." : ""}\n`)
    return check ? 0 : 1
  } else io.err(`${message ?? `Since answered ${answer.status}.`}\n`)
  return check ? 2 : 1
}

/**
 * The free scan of a public GitHub repository. A busy answer is waited out up to three times. A connection that drops is
 * asked again twice: a long scan can outlast a connection, and asking again costs nothing, since a scan still running is
 * joined and a finished one is served.
 */
async function publicScan(ctx, fullName) {
  let waits = 0
  let drops = 0
  for (;;) {
    const answer = await call(ctx, "POST", "/v1/scan", { body: { repo: fullName } })
    if (answer.status === 200 && answer.json && isObject(answer.json.repository)) return { result: answer.json }
    if (answer.status === 503 && errorOf(answer) === "busy" && waits < 3) {
      waits += 1
      const seconds = clamp(answer.retryAfter ?? 20, 1, 60)
      ctx.io.err(`Since is busy. Trying again in ${text.wait(seconds)}.\n`)
      await ctx.io.sleep(seconds * 1000)
      continue
    }
    if (answer.status === 0 && drops < 2) {
      drops += 1
      ctx.io.err("The connection dropped before the answer came. Asking again.\n")
      continue
    }
    return { answer }
  }
}

/** One keyed check, whole: waits while a connected repository is still being read, then follows every page (pages are free). */
async function checkAll(ctx, body) {
  const started = ctx.io.now()
  let answer = await call(ctx, "POST", "/v1/check", { body, key: ctx.key })
  while (answer.status === 200 && answer.json && answer.json.scanning && Array.isArray(answer.json.findings) && !answer.json.findings.length) {
    const seconds = clamp(wholeNumber(answer.json.scanning.retryAfterSec) ?? 10, 1, 30)
    if (ctx.io.now() - started + seconds * 1000 > MAX_WAIT_MS) break
    ctx.io.err(`Since is still reading this repository. Trying again in ${text.wait(seconds)}.\n`)
    await ctx.io.sleep(seconds * 1000)
    answer = await call(ctx, "POST", "/v1/check", { body, key: ctx.key })
  }
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.findings)) return answer
  const whole = answer.json
  for (let page = 1; page < MAX_PAGES && typeof whole.nextCursor === "string" && whole.nextCursor; page++) {
    const next = await call(ctx, "POST", "/v1/check", { body: { ...body, cursor: whole.nextCursor }, key: ctx.key })
    if (next.status !== 200 || !next.json || !Array.isArray(next.json.findings)) break
    whole.findings = whole.findings.concat(next.json.findings)
    whole.nextCursor = next.json.nextCursor ?? null
  }
  if (whole.nextCursor) whole.truncated = true
  return answer
}

/** A repository connected to the caller's workspace, checked where Since already has its code. */
async function connectedCheck(ctx, fullName) {
  const repos = await call(ctx, "GET", "/v1/repos", { key: ctx.key })
  if (repos.status !== 200 || !repos.json) return { exit: refuse(ctx, repos) }
  const list = Array.isArray(repos.json.repos) ? repos.json.repos : []
  const repo = list.find((entry) => String(entry.fullName).toLowerCase() === fullName.toLowerCase())
  if (!repo) return {}
  const answer = await checkAll(ctx, { repo: repo.id, detail: "summary" })
  if (answer.status === 403 && errorOf(answer) === "impact_api_not_in_plan") return {}
  if (answer.status !== 200 || !answer.json) return { exit: refuse(ctx, answer) }
  return { answer: answer.json, repo }
}

/** The manifests in this folder, sent one by one as the GitHub Action sends them. */
async function localCheck(ctx, noOrigin) {
  const { io, p, check } = ctx
  if (!ctx.key) {
    io.err(
      noOrigin
        ? `This folder has no GitHub origin. Name a public repository, as ${p} owner/repo, or sign in to check the files here: ${p} login\n`
        : `Checking the files in this folder needs a key. Sign in first: ${p} login\n`,
    )
    return check ? 2 : 1
  }
  const found = findManifests(io)
  if (!found.length) {
    io.err("No manifests in this folder (package.json, pyproject.toml, go.mod and the like).\n")
    return check ? 2 : 1
  }
  const files = []
  const notes = []
  let last = null
  if (found.length > ctx.maxFiles) {
    notes.push(`${text.plural(found.length, "manifest")} found, the first ${ctx.maxFiles} checked (each one costs credits). Raise it with --max-files.`)
  }
  for (const file of found.slice(0, ctx.maxFiles)) {
    const content = io.readFile(`${io.cwd}/${file}`)
    if (content === null) notes.push(`${file}: could not be read.`)
    else if (content.length > MAX_CHARACTERS) notes.push(`${file}: over ${text.num(MAX_CHARACTERS)} characters, not sent.`)
    else {
      const answer = await checkAll(ctx, checkBody(file, content))
      if (answer.status === 200 && answer.json && Array.isArray(answer.json.findings)) files.push({ path: file, answer: answer.json })
      else if (answer.status === 401 || answer.status === 402) return refuse(ctx, answer)
      else {
        last = answer
        const message = answer.json && typeof answer.json.message === "string" ? answer.json.message : null
        notes.push(`${file}: ${answer.status === 0 || answer.status >= 500 ? "Since could not be reached." : message ?? `Since answered ${answer.status}.`}`)
      }
    }
  }
  if (!files.length && last) return refuse(ctx, last)
  return report(ctx, { kind: "local", files, notes })
}

/** Print what was found and return the exit code. */
function report(ctx, data) {
  const { io, args, p, c, check } = ctx
  const all = Boolean(args.flags.all)
  let items
  if (data.kind === "scan") items = text.scanItems(Array.isArray(data.result.findings) ? data.result.findings : [])
  else if (data.kind === "repo") items = text.checkItems(data.answer.findings, null, p)
  else items = data.files.flatMap((file) => text.checkItems(file.answer.findings, file.path, p))
  if (ctx.within !== null) items = items.filter((item) => item.daysLeft !== null && item.daysLeft <= ctx.within)
  items = text.order(items)
  const failing = check ? items.filter((item) => text.fails(item, ctx.failOn)) : []
  const exit = failing.length ? 1 : 0
  if (args.flags.json) {
    const whole = data.kind === "scan" ? data.result : data.kind === "repo" ? data.answer : { files: data.files }
    io.out(`${JSON.stringify(whole, null, 2)}\n`)
    return exit
  }

  const lines = [""]
  const notes = []
  const footer = []
  const counts = text.countsLine(items, ctx.within)
  const more = `${p}${check ? " check" : ""}${args.positional.length ? ` ${args.positional[0]}` : ""} --all`
  if (data.kind === "scan") {
    const result = data.result
    const repository = result.repository
    const summary = isObject(result.summary) ? result.summary : {}
    const sha = String(repository.headSha ?? "").slice(0, 7)
    const branch = String(repository.defaultBranch ?? "")
    const parsed = Date.parse(String(result.scannedAt ?? ""))
    const scanned = text.ago(Number.isFinite(parsed) ? io.now() - parsed : null)
    const head = [c.bold(String(repository.fullName)), sha ? `${branch}@${sha}` : branch, scanned ? `scanned ${scanned}` : ""]
    lines.push(`  ${head.filter(Boolean).join("  ")}`)
    const dependencies = wholeNumber(summary.dependencies) ?? 0
    const files = wholeNumber(summary.files) ?? 0
    lines.push(`  ${counts}   ${text.num(dependencies)} ${dependencies === 1 ? "dependency" : "dependencies"} and ${text.plural(files, "file")} read`)
    if (data.fromTree) lines.push(c.dim(`  Read from GitHub (${branch}), not your working tree.`))
    const coverage = isObject(result.coverage) && Array.isArray(result.coverage.notes) ? result.coverage.notes : []
    for (const note of coverage) notes.push(String(note))
    footer.push(`  ${"Full report".padEnd(14)}${SITE}/scan/${repository.fullName}?utm_source=cli`)
  } else if (data.kind === "repo") {
    lines.push(`  ${[c.bold(String(data.repo.fullName)), String(data.repo.defaultBranch ?? ""), "connected repository"].filter(Boolean).join("  ")}`)
    lines.push(`  ${counts}   ${text.num(wholeNumber(data.answer.checkedCount) ?? 0)} checked`)
    for (const note of text.answerNotes([data.answer])) notes.push(note)
  } else {
    const checked = data.files.reduce((sum, file) => sum + (wholeNumber(file.answer.checkedCount) ?? 0), 0)
    lines.push(`  ${c.bold("This folder")}  ${text.plural(data.files.length, "manifest")} checked`)
    lines.push(`  ${counts}   ${text.num(checked)} checked`)
    for (const note of data.notes) notes.push(note)
    for (const note of text.answerNotes(data.files.map((file) => file.answer))) notes.push(note)
    notes.push(`Only manifests were checked. Connect the repository to check its code too: ${APP}?utm_source=cli`)
  }
  lines.push("")
  for (const line of text.findingLines(items, all, c, more)) lines.push(line)
  for (const note of notes) lines.push(`  ${note}`)
  if (notes.length) lines.push("")
  for (const line of footer) lines.push(line)
  if (!check) lines.push(`  ${"Next".padEnd(14)}${p} init   your coding agent checks before it writes`)
  else if (ctx.failOn !== "none") {
    const level = ctx.failOn === "critical" ? "at critical" : `at ${ctx.failOn} or above`
    lines.push(failing.length ? `  ${"Failed".padEnd(14)}${text.plural(failing.length, "finding")} ${level}` : `  ${"Passed".padEnd(14)}nothing ${level}`)
  }
  lines.push("")
  io.out(lines.join("\n"))
  return exit
}

/** `since`, `since <owner/repo>` and `since check`: find the repository, then the cheapest way to read it. */
async function repoCommand(ctx) {
  const { io, args, p, check } = ctx
  const given = args.positional.length ? args.positional[0] : null
  const fromTree = given === null
  const repo = parseRepo(fromTree ? io.origin() : given)
  const stop = check ? 2 : 1
  if (!fromTree && !repo) {
    io.err(`${given} is not a repository. Use owner/repo or a GitHub URL.\n`)
    return 2
  }
  if (!fromTree && args.flags.local) {
    io.err("--local checks the folder you are in, so it takes no repository name.\n")
    return 2
  }
  // In CI with a key, and whenever --local asks: the checkout itself, not the default branch on GitHub.
  if (fromTree && (args.flags.local || (check && ctx.key))) return localCheck(ctx, false)
  if (!repo || repo.host !== "github.com") {
    if (fromTree) return localCheck(ctx, true)
    io.err(`${given} is not on GitHub. Run ${p} inside its folder to check its manifests.\n`)
    return stop
  }

  const fullName = `${repo.owner}/${repo.name}`
  // A first scan takes up to a minute, and a silent terminal reads as a hung one.
  // Run inside a repository, the name comes from its origin remote: say so, or the name looks like part of the command.
  io.err(`Reading ${fromTree ? `this folder's repository (${fullName})` : fullName} on GitHub. A first scan can take a minute.\n`)
  const scan = await publicScan(ctx, fullName)
  if (scan.result) return report(ctx, { kind: "scan", result: scan.result, fromTree })
  // The free scan said no (not public, too big, or today's scans are used up): with a key there is still a way to answer
  // for the folder you are in, so say why and take it. Without one, or when Since itself is down, this is the end.
  const refusal = errorOf(scan.answer)
  const down = scan.answer.status === 0 || scan.answer.status >= 500
  if (refusal !== "not_found") {
    if (down || !ctx.key || !fromTree) return refuse(ctx, scan.answer)
    const why = scan.answer.json && typeof scan.answer.json.message === "string" ? `${scan.answer.json.message} ` : ""
    io.err(`${why}Checking with your key instead.\n`)
  }
  if (!ctx.key) {
    io.err(
      fromTree
        ? `${fullName} is not a public GitHub repository. For a private one, sign in and run this again: ${p} login\n`
        : `GitHub serves no public repository named ${fullName}. For a private one, sign in first: ${p} login\n`,
    )
    return stop
  }
  const connected = await connectedCheck(ctx, fullName)
  if (connected.answer) return report(ctx, { kind: "repo", answer: connected.answer, repo: connected.repo })
  if (connected.exit !== undefined) return connected.exit
  if (fromTree) return localCheck(ctx, false)
  io.err(`${fullName} is private and Since cannot read it from here. Run ${p} inside its folder.\n`)
  return stop
}

async function calendarCommand(ctx) {
  const { io, args, p, c } = ctx
  const within = ctx.within ?? 30
  const provider = args.flags.provider ?? null
  const answer = await call(ctx, "GET", "/v1/calendar", { query: { within: `${within}d`, provider, limit: args.flags.all ? 500 : 15 } })
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.entries)) return refuse(ctx, answer)
  if (args.flags.json) {
    io.out(`${JSON.stringify(answer.json, null, 2)}\n`)
    return 0
  }
  const entries = answer.json.entries
  const lines = ["", `  ${c.bold(`Ending in the next ${text.plural(within, "day")}`)}${provider ? `  ${provider}` : ""}`, ""]
  if (!entries.length) lines.push("  Nothing on the calendar in that time.")
  for (const line of text.calendarLines(entries, SITE, c)) lines.push(line)
  lines.push("")
  if (answer.json.nextCursor) lines.push(`  ${"More".padEnd(14)}${p} calendar${provider ? ` --provider ${provider}` : ""} --all`)
  lines.push(`  ${"Calendar".padEnd(14)}${SITE}/calendar?utm_source=cli`, "")
  io.out(lines.join("\n"))
  return 0
}

async function changedCommand(ctx) {
  const { io, args, p, c } = ctx
  const name = args.positional[0]
  if (!ctx.key) {
    io.err(`${p} changed needs a key. Sign in first: ${p} login\n`)
    return 1
  }
  const ecosystem = args.flags.ecosystem ?? "npm"
  const since = args.flags.since ?? null
  const answer = await call(ctx, "GET", "/v1/changes", { query: { package: name, ecosystem, since, limit: args.flags.all ? 200 : 20 }, key: ctx.key })
  if (answer.status !== 200 || !answer.json || !Array.isArray(answer.json.changes)) return refuse(ctx, answer)
  if (args.flags.json) {
    io.out(`${JSON.stringify(answer.json, null, 2)}\n`)
    return 0
  }
  const changes = text.uniqueChanges(answer.json.changes)
  const found = isObject(answer.json.package) ? answer.json.package : {}
  const head = [c.bold(String(found.name ?? name)), String(found.ecosystem ?? ecosystem), found.latest ? `latest ${found.latest}` : ""]
  const lines = ["", `  ${head.filter(Boolean).join("  ")}`]
  lines.push(`  ${changes.length ? text.plural(changes.length, "change") : "No changes recorded"}${since ? ` since ${since}` : ""}${answer.json.nextCursor ? ", and more" : ""}`, "")
  for (const line of text.changeLines(changes, c)) lines.push(line)
  if (changes.length) lines.push("")
  const notes = []
  if (answer.json.reading) notes.push("Since is reading newer releases of this package now. Run this again in a few minutes to include them.")
  for (const note of notes) lines.push(`  ${note}`)
  if (notes.length) lines.push("")
  if (answer.json.nextCursor) lines.push(`  ${"More".padEnd(14)}${p} changed ${name}${since ? ` --since ${since}` : ""}${ecosystem !== "npm" ? ` --ecosystem ${ecosystem}` : ""} --all`, "")
  io.out(lines.join("\n"))
  return 0
}

/** Sign in without a password: Since emails nothing here, the person approves the link in a browser and the key comes back. */
async function loginCommand(ctx) {
  const { io, args, p } = ctx
  let email = String(args.flags.email ?? "").trim()
  if (!email && io.tty) email = String(await io.prompt("Email: ")).trim()
  if (!email) {
    io.err(`Give the email to sign in with: ${p} login --email you@example.com\n`)
    return 2
  }
  const start = await call(ctx, "POST", "/v1/agent/register", { body: { email, client_name: "since-cli" } })
  const setup = start.json && isObject(start.json.setup) ? start.json.setup : null
  if (start.status !== 200 || !setup || typeof start.json.claim_token !== "string") return refuse(ctx, start)
  const link = String(setup.verification_uri_complete ?? setup.verification_uri)
  // The approval page shows an eight-character code in two halves: print it the way the person will see it there.
  const code = String(setup.user_code ?? "")
  io.out(`\n  Open this link to approve the sign-in:\n    ${link}\n  Code: ${code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code}\n\n`)
  if (io.tty) io.open(link)
  let interval = clamp(wholeNumber(setup.interval) ?? 5, 1, 60)
  const deadline = io.now() + clamp(wholeNumber(setup.expires_in) ?? 900, 30, 3600) * 1000
  while (io.now() + interval * 1000 <= deadline) {
    await io.sleep(interval * 1000)
    const answer = await call(ctx, "POST", "/v1/agent/token", { body: { claim_token: start.json.claim_token } })
    if (answer.status === 200 && answer.json && typeof answer.json.api_key === "string") {
      const file = credentialsPath(io)
      const stored = { api: ctx.api, api_key: answer.json.api_key, key_name: answer.json.key_name ?? null, key_prefix: answer.json.key_prefix ?? null }
      io.writeFile(file, `${JSON.stringify(stored, null, 2)}\n`, 0o600)
      io.out(`  Signed in. The key is stored in ${file}\n\n`)
      return 0
    }
    const code = errorOf(answer)
    if (code === "slow_down") interval += 5
    else if (code === "expired_token") break
    else if (code === "access_denied") {
      io.err("The sign-in was declined.\n")
      return 1
    } else if (code !== "authorization_pending" && answer.status !== 0 && answer.status < 500) return refuse(ctx, answer)
  }
  io.err(`The link expired before it was approved. Run ${p} login again.\n`)
  return 1
}

function logoutCommand(ctx) {
  const { io } = ctx
  const removed = io.remove(credentialsPath(io))
  io.out(removed ? `Signed out. The key is gone from this computer; delete it for good at ${APP}/api-keys\n` : "No stored key.\n")
  return 0
}

export async function run(argv, io) {
  const args = parseArgs(argv)
  const p = io.program
  const usage = (message) => {
    io.err(`${message}\nRun ${p} --help for the commands.\n`)
    return 2
  }
  if (args.error) return usage(args.error)
  if (args.flags.version || args.command === "version") {
    io.out(`since-dev ${VERSION}\n`)
    return 0
  }
  if (args.flags.help || args.command === "help") {
    io.out(help(p))
    return 0
  }
  const command = args.command
  const words = args.positional
  if (command === "changed") {
    if (words.length !== 1) return usage(words.length ? `Too many words: ${words[1]}` : `${p} changed needs a package name, as ${p} changed react`)
  } else if (command === null || command === "check") {
    if (words.length > 1) return usage(`Too many words: ${words[1]}`)
    if (command === null && words.length === 1 && !words[0].includes("/")) return usage(`Unknown command: ${words[0]}`)
  } else if (words.length) return usage(`Too many words: ${words[0]}`)

  const failOn = args.flags["fail-on"] ?? "high"
  if (!FAIL_ON.includes(failOn)) return usage("--fail-on takes low, medium, high, critical or none.")
  let within = null
  if (args.flags.within !== undefined) {
    if (!/^\d{1,4}d?$/.test(args.flags.within) || Number.parseInt(args.flags.within, 10) < 1) return usage("--within takes a number of days, as 30d.")
    within = Number.parseInt(args.flags.within, 10)
  }
  let maxFiles = 10
  if (args.flags["max-files"] !== undefined) {
    if (!/^\d{1,3}$/.test(args.flags["max-files"]) || Number.parseInt(args.flags["max-files"], 10) < 1) return usage("--max-files takes a number from 1 to 999.")
    maxFiles = Number.parseInt(args.flags["max-files"], 10)
  }

  const api = String(io.env.SINCE_API_URL || DEFAULT_API).replace(/\/+$/, "")
  const color = Boolean(io.tty) && !args.flags["no-color"] && !io.env.NO_COLOR && io.env.TERM !== "dumb"
  const ctx = { io, args, p, api, c: text.palette(color), check: command === "check", key: readKey(io, api), failOn, within, maxFiles }
  if (command === "calendar") return calendarCommand(ctx)
  if (command === "changed") return changedCommand(ctx)
  if (command === "init") return init(io, p, Boolean(args.flags["dry-run"]))
  if (command === "login") return loginCommand(ctx)
  if (command === "logout") return logoutCommand(ctx)
  return repoCommand(ctx)
}
