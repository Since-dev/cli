// What the CLI reads from the folder it runs in: which repository this is, and which manifests to send.
// The manifest rules are the GitHub Action's (distribution/action/index.mjs): the same names, the same folders skipped.

export const PATTERNS = [
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
const LOCKFILES = new Set([
  "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
  "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
  "packages.lock.json", "gradle.lockfile",
])
const SKIP = new Set([".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "target", ".next", "__pycache__"])
export const MAX_CHARACTERS = 500_000

/** owner and name from "owner/repo", a GitHub URL or a git remote; null for anything else. A bare "owner/repo" means GitHub. */
export function parseRepo(text) {
  const value = String(text ?? "").trim()
  if (!value) return null
  const short = /^([A-Za-z0-9_-][A-Za-z0-9_.-]*)\/([A-Za-z0-9_.-]+)$/.exec(value)
  if (short) return { host: "github.com", owner: short[1], name: short[2].replace(/\.git$/, "") }
  const remote =
    /^(?:https?:\/\/|ssh:\/\/|git:\/\/)?(?:[^@\/]+@)?([A-Za-z0-9.-]+\.[A-Za-z]+)(?::\d+)?[:\/]([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?(?:\/.*)?$/.exec(value)
  if (!remote) return null
  return { host: remote[1].toLowerCase(), owner: remote[2], name: remote[3] }
}

export function patternRegExp(pattern) {
  const source = pattern
    .replace(/^\.\//, "")
    .split(/(\*\*\/|\*)/)
    .map((part) => (part === "**/" ? "(?:.*/)?" : part === "*" ? "[^/]*" : part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")))
    .join("")
  return new RegExp(`^${source}$`)
}

/** The manifests under the folder, nearest the top first, then by name. */
export function findManifests(io) {
  const patterns = PATTERNS.map(patternRegExp)
  const found = []
  const walk = (folder) => {
    for (const entry of io.list(folder ? `${io.cwd}/${folder}` : io.cwd)) {
      const relative = folder ? `${folder}/${entry.name}` : entry.name
      if (entry.dir) {
        if (!SKIP.has(entry.name)) walk(relative)
      } else if (patterns.some((pattern) => pattern.test(relative))) found.push(relative)
    }
  }
  walk("")
  const depth = (file) => file.split("/").length
  return found.sort((a, b) => depth(a) - depth(b) || (a < b ? -1 : a > b ? 1 : 0))
}

export function checkBody(file, content) {
  const name = file.split("/").pop()
  return { [LOCKFILES.has(name) ? "lockfile" : "manifest"]: { path: file, content }, detail: "summary" }
}
