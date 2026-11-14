// The real world behind the CLI: the network, the folder, git, the terminal. The tests swap all of it for a fixture.

import { execFileSync, spawn } from "node:child_process"
import { chmodSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import path from "node:path"
import { createInterface } from "node:readline"

function git(cwd, args) {
  try {
    return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim() || null
  } catch {
    return null
  }
}

export function realIo() {
  const cwd = process.cwd()
  return {
    runtime: "node",
    // Run through npx there is no `since` on the PATH, so the hints name the command that works again.
    program: /[\\/]_npx[\\/]/.test(process.argv[1] ?? "") ? "npx since-dev" : "since",
    env: process.env,
    cwd,
    home: homedir(),
    platform: process.platform,
    tty: Boolean(process.stdout.isTTY),
    out: (text) => void process.stdout.write(text),
    err: (text) => void process.stderr.write(text),
    now: () => Date.now(),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    async fetch({ method, url, headers, body }) {
      // The API never redirects, so a redirect is never followed: a key goes to the host it was meant for and nowhere else.
      const response = await fetch(url, { method, headers, body: body ?? undefined, redirect: "manual", signal: AbortSignal.timeout(120_000) })
      return { status: response.status, retryAfter: response.headers.get("retry-after"), text: await response.text() }
    },
    origin: () => git(cwd, ["remote", "get-url", "origin"]),
    root: () => git(cwd, ["rev-parse", "--show-toplevel"]) ?? cwd,
    open(url) {
      if (process.env.CI) return
      const [command, args] =
        process.platform === "darwin" ? ["open", [url]] : process.platform === "win32" ? ["rundll32", ["url.dll,FileProtocolHandler", url]] : ["xdg-open", [url]]
      try {
        const child = spawn(command, args, { stdio: "ignore", detached: true })
        child.on("error", () => {})
        child.unref()
      } catch {
        // No browser to open: the link is printed, which is enough.
      }
    },
    prompt(question) {
      const terminal = createInterface({ input: process.stdin, output: process.stderr })
      // Input that ends with no answer (Ctrl-D, a closed pipe) is no answer, and the caller still hears back: without this the
      // question never settles and the command ends in silence.
      return new Promise((resolve) => {
        terminal.once("close", () => resolve(null))
        terminal.question(question, (answer) => {
          resolve(answer)
          terminal.close()
        })
      })
    },
    readFile(file) {
      try {
        return readFileSync(file, "utf8")
      } catch {
        return null
      }
    },
    /** mode is null for a file of the repository, or the permission bits for a private one (its folder is made private too). */
    writeFile(file, text, mode) {
      mkdirSync(path.dirname(file), mode === null ? { recursive: true } : { recursive: true, mode: 0o700 })
      writeFileSync(file, text, mode === null ? {} : { mode })
      if (mode !== null) chmodSync(file, mode)
    },
    remove(file) {
      try {
        rmSync(file)
        return true
      } catch {
        return false
      }
    },
    /** A folder's entries by name; a link is never followed into a folder. */
    list(folder) {
      try {
        return readdirSync(folder, { withFileTypes: true })
          .map((entry) => ({ name: entry.name, dir: entry.isDirectory() }))
          .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
      } catch {
        return []
      }
    },
  }
}
