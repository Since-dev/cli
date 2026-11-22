// The coding agents `since hook` runs under (Features plan step 25): each one's hook input mapped onto Claude Code's shape, which the hooks
// read, and each one's answer, worded the way it takes it. From each agent's own hook documentation, read Nov 21, 2026:
//   Cursor      cursor.com/docs/hooks: postToolUse (matcher Write) answers `additional_context`; stop answers `followup_message`, sent as
//               the next user message; sessionStart answers `additional_context`; a stop input counts its rounds in `loop_count`.
//   Codex       learn.chatgpt.com/docs/hooks: an edit arrives as PostToolUse with tool_name `apply_patch` and the patch in
//               `tool_input.command`; answers in `hookSpecificOutput.additionalContext`; Stop continues only on `decision: "block"`.
//   Gemini CLI  geminicli.com/docs/hooks: AfterTool on `write_file` (`file_path`, `content`) and `replace` (`file_path`, `old_string`,
//               `new_string`) answers `hookSpecificOutput.additionalContext`; AfterAgent sends a new prompt on `decision: "deny"`.
// Claude Code needs no mapping.

export const CLIENTS = ["claude", "cursor", "codex", "gemini"]

/** The files a Codex patch adds or changes: each with its path and the lines the patch adds, in order. */
export function patchedFiles(patch) {
  const text = String(patch ?? "")
  const begin = text.indexOf("*** Begin Patch")
  if (begin < 0) return []
  const files = []
  let current = null
  for (const line of text.slice(begin).split("\n")) {
    const header = /^\*\*\* (Add|Update|Delete) File: (.+?)\s*$/.exec(line)
    const moved = /^\*\*\* Move to: (.+?)\s*$/.exec(line)
    if (header) {
      current = header[1] === "Delete" ? null : { path: header[2], added: [], whole: header[1] === "Add" }
      if (current) files.push(current)
    } else if (moved && current) current.path = moved[1]
    else if (line.startsWith("*** End Patch")) break
    else if (current && line.startsWith("+")) current.added.push(line.slice(1))
  }
  return files
}

/**
 * The hook event as the hooks read it (Claude Code's shape): one event for a write or an edit, several for a Codex patch that touches
 * several files. Claude Code's own event is returned as it is.
 */
export function claudeEvents(client, event) {
  if (!event || typeof event !== "object") return []
  const base = { session_id: event.session_id ?? event.conversation_id ?? null, cwd: event.cwd ?? (Array.isArray(event.workspace_roots) ? event.workspace_roots[0] : null) }
  if (client === "codex" && event.tool_name === "apply_patch") {
    return patchedFiles(event.tool_input?.command).map((file) => file.whole
      ? { ...event, ...base, tool_name: "Write", tool_input: { file_path: file.path, content: file.added.join("\n") } }
      : { ...event, ...base, tool_name: "Edit", tool_input: { file_path: file.path, edits: file.added.filter((line) => line.trim()).map((line) => ({ new_string: line })) } })
  }
  if (client === "gemini" && (event.tool_name === "write_file" || event.tool_name === "replace")) {
    const input = event.tool_input ?? {}
    return [event.tool_name === "write_file"
      ? { ...event, ...base, tool_name: "Write", tool_input: { file_path: input.file_path, content: input.content } }
      : { ...event, ...base, tool_name: "Edit", tool_input: { file_path: input.file_path, new_string: input.new_string, replace_all: Boolean(input.expected_replacements > 1) } }]
  }
  if (client === "cursor" && event.tool_name !== undefined) {
    // Cursor names every file edit Write; its tool input's fields are not documented, so each spelling seen in the wild is read, and an
    // edit that names no text is read as a whole file.
    const input = event.tool_input ?? {}
    const file = input.file_path ?? input.path ?? input.target_file ?? input.filePath ?? null
    const content = input.content ?? input.contents ?? input.file_text ?? null
    return [{ ...event, ...base, tool_name: "Write", tool_input: { file_path: file, ...(typeof content === "string" ? { content } : {}) } }]
  }
  return [{ ...event, ...base, stop_hook_active: event.stop_hook_active === true || (typeof event.loop_count === "number" && event.loop_count > 0) }]
}

/** The answer to a write's hook, as the agent takes it. */
export function writeAnswer(client, note) {
  if (client === "cursor") return { additional_context: note }
  return { hookSpecificOutput: { hookEventName: client === "gemini" ? "AfterTool" : "PostToolUse", additionalContext: note } }
}

/**
 * The answer to the finish check, as the agent takes it. Claude Code takes a note without going on, or `decision: "block"`; the others can
 * only go on (Cursor's follow-up message, Codex's block, Gemini CLI's deny, each a new turn), so they answer only when the team chose to
 * block, and say nothing otherwise.
 */
export function stopAnswer(client, note, block) {
  if (client === "claude") return block ? { decision: "block", reason: note } : { hookSpecificOutput: { hookEventName: "Stop", additionalContext: note } }
  if (!block) return null
  if (client === "cursor") return { followup_message: note }
  if (client === "gemini") return { decision: "deny", reason: note }
  return { decision: "block", reason: note }
}

/** The answer to the session brief, as the agent takes it. */
export function briefAnswer(client, card) {
  if (client === "cursor") return { additional_context: card }
  return { hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: card } }
}
