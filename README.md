# since-dev

Find the code in a repository that is about to stop working: AI model IDs with a shutdown date, runtimes past end of life, dependencies with security advisories or breaking releases. Every finding comes with its file and line, its date, its replacement and its source. When a replacement model works differently (a default that changed, a setting it refuses), the finding says so under it.

```bash
npx since-dev
```

Run it inside any repository. A public GitHub repository needs no sign-in and no setup.

```
  since-dev-lab/demo-shop  main@fd7aa13  scanned just now
  3 with a date   1 undated   2 to review   4 dependencies and 7 files read
  Read from GitHub (main), not your working tree.

  20 days   claude-sonnet-4-5-20250929 on Claude API: retirement 2026-11-30
            src/ai/client.ts:6  and 1 more   use claude-sonnet-5-5
            https://platform.claude.com/docs/en/about-claude/model-deprecations

  31 days   gpt-5 on OpenAI API: retirement 2026-12-11
            src/ai/summarize.ts:8   use gpt-5.6-sol
            https://developers.openai.com/api/docs/deprecations

  passed    Node.js 20: end of life 2026-04-30
            .github/workflows/ci.yml   use 24
            https://endoflife.date/nodejs

  medium    GHSA-qw6h-vgh9-j6wx: express vulnerable to XSS via response.redirect()
            src/server.ts:1   use 4.20.0
            https://osv.dev/vulnerability/GHSA-qw6h-vgh9-j6wx

  review    openai: 27 changes in newer releases may reach this code
            src/ai/summarize.ts:7

  review    @anthropic-ai/sdk: 6 changes in newer releases may reach this code
            src/ai/client.ts:9  and 1 more

  Full report   https://since.dev/scan/since-dev-lab/demo-shop?utm_source=cli
  Next          npx since-dev init   your coding agent checks before it writes
```

That is the output for [a small demo repository](https://github.com/since-dev-lab/demo-shop) on November 10, 2026. To see it yourself: `npx since-dev since-dev-lab/demo-shop`.

## Run it

| With | Command |
| --- | --- |
| Node 18 or newer | `npx since-dev` |
| uv | `uvx since-dev` |
| pipx | `pipx run since-dev` |

To keep it, `npm install -g since-dev` or `pip install since-dev` gives you the command `since`. The npm and PyPI packages are the same CLI: no dependencies in either, and one set of tests holds both to the same output, character for character.

## Commands

| Command | What it does |
| --- | --- |
| `since` | Checks the repository you are in. It reads the `origin` remote to know which one. |
| `since <owner/repo>` | Checks any public GitHub repository. A GitHub URL works too. |
| `since init` | Sets up your coding agent to check before it writes. See below. |
| `since calendar` | What ends in the next 30 days: model retirements, runtime end of life, API removals. `--within 90d`, `--provider openai`. |
| `since changed <package>` | Breaking changes and deprecations of a package. `--since 4.19.2` lists only what changed after that version, `--ecosystem pypi` picks the registry. Needs a key. |
| `since check` | The same check for CI: exit 1 when a finding fails. See below. |
| `since login`, `since logout` | Sign in for private repositories, and remove the stored key. |

`--all` shows every finding (10 by default), `--json` prints the answer as JSON, `--within 30d` keeps only dates within that many days.

Findings are listed with the next deadline first, then dates that have passed, then security advisories with no date, then the ones marked `review`: Since found the name in your code but could not prove the change reaches it. When a dependency has many of those at one line, they print as one row with the count.

## Set up your coding agent

Coding agents write the model IDs and versions that were current when they were trained. This makes yours check first:

```bash
npx since-dev init
```

It shows the files it would write and asks before it writes them:

| File | For |
| --- | --- |
| `.mcp.json` | Claude Code |
| `.cursor/mcp.json` | Cursor |
| `.vscode/mcp.json` | VS Code |
| `.claude/rules/since.md` | Claude Code's rule |
| `.cursor/rules/since.mdc` | Cursor's rule |
| `GEMINI.md` | Gemini CLI's rule, between two markers |
| `AGENTS.md` | The rule Codex and other agents read, between two markers |

The first three connect the Since.dev tools. The other four hold one short rule that says when to check: before the agent writes a model ID, adds or upgrades a dependency, or writes a runtime version. Connecting the tools alone leaves it to the agent to think of them. The rule is what makes it check without being asked.

`--yes` writes without asking, for a script. `--dry-run` only shows what it would write.

Commit the files and everyone who clones the repository gets the same setup. No key goes in any file: the first tool call opens a sign-in in the browser.

It only ever adds its own: the `since` entry in the three config files, its two rule files, and its marked block in `GEMINI.md` and `AGENTS.md`. Everything else in those files stays as it is, a second run changes nothing, and a file it cannot read as plain JSON is left alone with the lines to add by hand.

## What it sends

- **A public GitHub repository:** only its name. Since reads the default branch from GitHub, so uncommitted work and other branches are not in the answer. It is free: 25 scans a day per address, and a scan is kept for an hour.
- **A private repository, after `since login`:** if the repository is connected to your Since workspace and your plan includes it, the answer comes from there. Otherwise the CLI sends the manifests it finds in the folder (`package.json`, `requirements*.txt`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`, `composer.json`, `pom.xml`, `build.gradle`, `*.csproj`), 10 at most unless you raise `--max-files`. Source files are never sent. Each manifest is one call to the API, which uses credits.
- `--local` checks the manifests in the folder even when the repository is public, for work you have not pushed.

## In CI

```yaml
- run: npx since-dev check --fail-on high
  env:
    SINCE_DEV_KEY: ${{ secrets.SINCE_DEV_KEY }}
```

With a key it checks the manifests of the checkout. Without one it reads the default branch of a public repository from GitHub, and says so.

| Exit code | Meaning |
| --- | --- |
| 0 | Nothing proven at or above `--fail-on` (`low`, `medium`, `high` by default, `critical`, or `none`). |
| 1 | At least one finding fails. A finding marked `review` never fails a run. |
| 2 | Something for you to fix: a refused key, a bad option, no manifests. |

If Since cannot be reached, or the free scans for the day are used up, the run passes with a note. An outage on our side never fails your build.

On GitHub Actions, [since-dev/check](https://github.com/since-dev/check) does the same with annotations on the files and a table in the run summary.

## Sign in

```bash
since login
```

It asks for your email, prints a link, and waits while you approve it in the browser. The key is stored in `~/.config/since/credentials.json`, readable only by you. Setting `SINCE_DEV_KEY` works as well and wins over the stored key. `since logout` removes the stored key.

## Develop

```bash
node --test test.mjs
python3 -m unittest test_cli
```

Each file in `fixtures/` is one run: the arguments, the folder, and the answers the API gives. `expected/` holds what that run prints, requests and writes. Both test suites replay every fixture and compare with the same expected file. After a change to the Node side, `UPDATE_EXPECTED=1 node --test test.mjs` rewrites the expected files: read the diff, then make the Python side match.

## License

MIT. The data comes from [Since.dev](https://since.dev): the [calendar](https://since.dev/calendar) of what ends when, and the [API](https://since.dev/docs) behind this CLI.
