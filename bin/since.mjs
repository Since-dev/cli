#!/usr/bin/env node
import { run } from "../src/cli.mjs"
import { realIo } from "../src/io.mjs"

// Node 18 warns that fetch is experimental on its first call. It works, and the warning would land in the middle of the output.
const emitWarning = process.emitWarning
process.emitWarning = (warning, ...rest) => {
  const type = rest[0] && typeof rest[0] === "object" ? rest[0].type : rest[0]
  if (type === "ExperimentalWarning") return
  emitWarning.call(process, warning, ...rest)
}

run(process.argv.slice(2), realIo()).then(
  (code) => {
    process.exitCode = code
  },
  (error) => {
    process.stderr.write(`since: ${error && error.message ? error.message : error}\n`)
    process.exitCode = 1
  },
)
