// Text only: every function here turns an API answer into lines. No network, no files, no clock.
// since_dev/render.py is the same code in Python, and the shared fixtures hold both to the same output.

export const SEVERITIES = ["low", "medium", "high", "critical"]
const PAD = " ".repeat(12)
const SHOWN = 10

export function palette(on) {
  const paint = (code) => (text) => (on ? `\x1b[${code}m${text}\x1b[0m` : text)
  return { bold: paint(1), dim: paint(2), red: paint(31), yellow: paint(33), plain: (text) => text }
}

export const num = (value) => String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
export const plural = (count, word) => `${num(count)} ${word}${count === 1 ? "" : "s"}`

/** How long to wait, in words: seconds under a minute and a half, then minutes, then hours, rounded up. */
export function wait(seconds) {
  if (seconds < 90) return plural(Math.max(1, seconds), "second")
  if (seconds < 5400) return plural(Math.ceil(seconds / 60), "minute")
  return plural(Math.ceil(seconds / 3600), "hour")
}

/** How long ago, from milliseconds; "" when the time is not known. */
export function ago(ms) {
  if (ms === null || !Number.isFinite(ms)) return ""
  const minutes = Math.floor(ms / 60000)
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${plural(hours, "hour")} ago`
  return `${plural(Math.floor(hours / 24), "day")} ago`
}

const placeOf = (path, line) => (typeof line === "number" && line > 0 ? `${path}:${line}` : String(path))

/**
 * A public scan's findings, as the rows the CLI prints. A dependency that is a few majors behind can have dozens of
 * changes Since could not prove reach the code, all at the one line that imports it: those become one row.
 */
export function scanItems(findings) {
  const rows = []
  const groups = new Map()
  for (const finding of findings) {
    const where = Array.isArray(finding.where) ? finding.where.filter((place) => place && place.path) : []
    const source = finding.source && typeof finding.source === "object" ? finding.source.url : null
    const surface = finding.surface && typeof finding.surface === "object" ? finding.surface : {}
    const name = String(surface.name || "")
    const row = {
      title: String(finding.title ?? ""),
      review: finding.state === "needs_review",
      severity: finding.severity,
      daysLeft: typeof finding.daysLeft === "number" ? finding.daysLeft : null,
      place: where.length ? placeOf(where[0].path, where[0].line) : null,
      extra: Math.max(0, where.length - 1) + (typeof finding.more === "number" ? finding.more : 0),
      replacement: finding.replacement || null,
      source: source || null,
      hint: null,
    }
    if (!row.review || row.daysLeft !== null || !name) {
      rows.push(row)
      continue
    }
    const key = `${surface.key || name}|${row.place || ""}`
    const group = groups.get(key)
    if (group) group.members.push(row)
    else {
      const created = { members: [row], name }
      groups.set(key, created)
      rows.push(created)
    }
  }
  return rows.map((row) => {
    if (!row.members) return row
    if (row.members.length === 1) return row.members[0]
    const first = row.members[0]
    return { ...first, title: `${row.name}: ${num(row.members.length)} changes in newer releases may reach this code`, replacement: null, source: null }
  })
}

/** Many undated findings about one dependency at one place, as one row: the count, the worst severity, and how to list them. */
function mergeRows(group, program) {
  const proven = group.members.filter((row) => !row.review)
  const pool = proven.length ? proven : group.members
  let severity = pool[0].severity
  for (const row of pool) if (SEVERITIES.indexOf(row.severity) > SEVERITIES.indexOf(severity)) severity = row.severity
  const tally = [...SEVERITIES].reverse().map((level) => [level, group.members.filter((row) => row.severity === level).length]).filter(([, count]) => count)
  const unrated = group.members.length - tally.reduce((sum, [, count]) => sum + count, 0)
  if (unrated) tally.push(["unrated", unrated])
  const breakdown = tally.length > 1 ? ` (${tally.map(([level, count]) => `${num(count)} ${level}`).join(", ")})` : ""
  const since = group.version ? ` --since ${group.version}` : ""
  const registry = group.ecosystem && group.ecosystem !== "npm" ? ` --ecosystem ${group.ecosystem}` : ""
  return {
    title: `${group.subject}: ${num(group.members.length)} changes between this version and the latest${breakdown}`,
    review: !proven.length,
    severity,
    daysLeft: null,
    place: group.members[0].place,
    extra: 0,
    replacement: null,
    source: null,
    hint: group.isPackage && group.name ? `${program} changed ${group.name}${since}${registry}` : null,
  }
}

/**
 * A keyed check's findings as the same rows. They carry no title, so one is built from the name, the version and the reason.
 * A manifest check answers with every recorded change between the pinned version and the latest, hundreds for an old pin:
 * the undated ones about one dependency at one place become one row, and `since changed` lists them.
 */
export function checkItems(findings, fallbackPath, program) {
  const rows = []
  const groups = new Map()
  for (const finding of findings) {
    const surface = finding.surface && typeof finding.surface === "object" ? finding.surface : {}
    const name = String(surface.name || "")
    const version = finding.version || null
    const subject = [name, version].filter(Boolean).join(" ")
    const reason = String(finding.reason ?? "").trim()
    const where = Array.isArray(finding.where) ? finding.where.filter((place) => place && place.file) : []
    const first = where.length ? placeOf(where[0].file, where[0].line) : fallbackPath
    const source = finding.source && typeof finding.source === "object" ? finding.source.url : finding.source
    const row = {
      title: name && reason.toLowerCase().includes(name.toLowerCase()) ? reason : [subject, reason].filter(Boolean).join(": "),
      review: finding.state === "needs_review",
      severity: finding.severity,
      daysLeft: typeof finding.daysLeft === "number" ? finding.daysLeft : null,
      place: first || null,
      extra: Math.max(0, (typeof finding.placeCount === "number" ? finding.placeCount : where.length) - 1),
      replacement: finding.replacement || null,
      source: typeof source === "string" && source ? source : null,
      hint: null,
    }
    if (row.daysLeft !== null) {
      rows.push(row)
      continue
    }
    const key = `${surface.key || name}|${version || ""}|${row.place || ""}`
    const group = groups.get(key)
    if (group) group.members.push(row)
    else {
      const created = { members: [row], subject, name, version, isPackage: surface.kind === "package", ecosystem: surface.ecosystem || null }
      groups.set(key, created)
      rows.push(created)
    }
  }
  return rows.map((row) => (row.members ? (row.members.length === 1 ? row.members[0] : mergeRows(row, program)) : row))
}

/** Dated findings first (the next deadline, then the ones already passed, newest first), then undated, then the ones to review. */
export function order(items) {
  const group = (item) => (item.review ? 2 : item.daysLeft === null ? 1 : 0)
  const within = (item) => (group(item) !== 0 ? 0 : item.daysLeft >= 0 ? item.daysLeft : 100000 - item.daysLeft)
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => group(a.item) - group(b.item) || within(a.item) - within(b.item) || a.index - b.index)
    .map((entry) => entry.item)
}

/** The CI rule, the same as the GitHub Action's: proven, and at or above the severity asked for. */
export function fails(item, failOn) {
  const threshold = SEVERITIES.indexOf(failOn)
  return threshold >= 0 && !item.review && SEVERITIES.indexOf(item.severity) >= threshold
}

export function dayLabel(daysLeft) {
  if (daysLeft < 0) return "passed"
  if (daysLeft === 0) return "today"
  return daysLeft === 1 ? "1 day" : `${num(daysLeft)} days`
}

function label(item) {
  if (item.review) return "review"
  if (item.daysLeft === null) return SEVERITIES.includes(item.severity) ? item.severity : "no date"
  return dayLabel(item.daysLeft)
}

function dayTone(daysLeft, c) {
  return daysLeft <= 30 ? c.red : daysLeft <= 90 ? c.yellow : c.plain
}

function tone(item, c) {
  if (item.review) return c.dim
  if (item.daysLeft === null) return item.severity === "critical" || item.severity === "high" ? c.yellow : c.plain
  return dayTone(item.daysLeft, c)
}

/** "3 with a date   2 undated   1 to review", without the counts that are zero. */
export function countsLine(items, within) {
  const dated = items.filter((item) => !item.review && item.daysLeft !== null).length
  const undated = items.filter((item) => !item.review && item.daysLeft === null).length
  const review = items.filter((item) => item.review).length
  const parts = []
  if (dated) parts.push(`${num(dated)} with a date`)
  if (undated) parts.push(`${num(undated)} undated`)
  if (review) parts.push(`${num(review)} to review`)
  if (parts.length) return parts.join("   ")
  return within === null ? "No findings" : `Nothing dated within ${plural(within, "day")}`
}

/** The findings, three lines each: when and what, where and the replacement, the source. */
export function findingLines(items, all, c, moreHint) {
  const lines = []
  const shown = all ? items : items.slice(0, SHOWN)
  for (const item of shown) {
    lines.push(`  ${tone(item, c)(label(item).padEnd(10))}${item.title}`)
    const place = item.place ? item.place + (item.extra ? `  and ${num(item.extra)} more` : "") : null
    const detail = [place, item.replacement ? `use ${item.replacement}` : null, item.hint ? `list them: ${item.hint}` : null].filter(Boolean).join("   ")
    if (detail) lines.push(c.dim(PAD + detail))
    if (item.source) lines.push(c.dim(PAD + item.source))
    lines.push("")
  }
  if (items.length > shown.length) lines.push(`  and ${num(items.length - shown.length)} more   ${moreHint}`, "")
  return lines
}

/** What keyed answers say about their own gaps: still being read, cut short, or names that did not resolve. */
export function answerNotes(answers) {
  const notes = []
  let scanning = 0
  let reading = false
  let releases = 0
  let truncated = false
  const unresolved = []
  for (const answer of answers) {
    if (answer.scanning) scanning = Math.max(scanning, Number(answer.scanning.retryAfterSec) || 60)
    if (answer.reading) {
      reading = true
      releases += Number(answer.reading.releases) || 0
    }
    if (answer.truncated) truncated = true
    for (const entry of Array.isArray(answer.unresolved) ? answer.unresolved : []) unresolved.push(entry)
  }
  if (scanning) notes.push(`Since is still reading this repository. Run this again in ${wait(scanning)}.`)
  if (reading) {
    const what = releases ? plural(releases, "newer release") : "newer releases"
    notes.push(`Since is reading ${what} now. Run this again in a few minutes to include them.`)
  }
  if (truncated) notes.push("The answer was cut short: there are more findings than one answer holds.")
  if (unresolved.length) {
    const names = unresolved.slice(0, 3).map((entry) => String(entry.name)).join(", ")
    notes.push(`Not fully checked yet: ${names}${unresolved.length > 3 ? ` and ${num(unresolved.length - 3)} more` : ""}. --json says why.`)
  }
  return notes
}

/** One line per calendar entry, with its replacement and its page under it. */
export function calendarLines(entries, site, c) {
  const lines = []
  for (const entry of entries) {
    const days = typeof entry.daysLeft === "number" ? entry.daysLeft : null
    const when = days === null ? "no date" : dayLabel(days)
    const paint = days === null ? c.plain : dayTone(days, c)
    lines.push(`  ${paint(when.padEnd(10))}${entry.title}`)
    const link = typeof entry.path === "string" && entry.path.startsWith("/") ? `${site}${entry.path}?utm_source=cli` : null
    const detail = [entry.replacement ? `use ${entry.replacement}` : null, link].filter(Boolean).join("   ")
    if (detail) lines.push(c.dim(PAD + detail))
  }
  return lines
}

/** One line per change of a package, with the version, the date and the source under it. */
export function uniqueChanges(changes) {
  // One change can be recorded once per function it touches, with the same words: say it once.
  const seen = new Set()
  return changes.filter((change) => {
    const key = `${change.kind}|${change.title}|${change.introducedIn || change.removedIn}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function changeLines(changes, c) {
  const lines = []
  for (const change of changes) {
    const version = change.introducedIn || change.removedIn
    lines.push(`  ${String(change.kind ?? "").replace(/_/g, " ").padEnd(19)}${change.title}`)
    const evidence = Array.isArray(change.evidence) && change.evidence.length ? change.evidence[0].url : null
    const detail = [version ? `in ${version}` : null, change.effectiveDate || null, evidence || null].filter(Boolean).join("   ")
    if (detail) lines.push(c.dim(" ".repeat(21) + detail))
  }
  return lines
}
