"""The coding agents `since hook` runs under (Features plan step 25): each one's hook input mapped onto Claude Code's shape, which the
hooks read, and each one's answer, worded the way it takes it. src/clients.mjs is the same, with the documentation it follows."""

import re

CLIENTS = ["claude", "cursor", "codex", "gemini"]


def patched_files(patch):
    """The files a Codex patch adds or changes: each with its path and the lines the patch adds, in order."""
    text = str(patch or "")
    begin = text.find("*** Begin Patch")
    if begin < 0:
        return []
    files = []
    current = None
    for line in text[begin:].split("\n"):
        header = re.match(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", line)
        moved = re.match(r"^\*\*\* Move to: (.+?)\s*$", line)
        if header:
            current = None if header.group(1) == "Delete" else {"path": header.group(2), "added": [], "whole": header.group(1) == "Add"}
            if current:
                files.append(current)
        elif moved and current:
            current["path"] = moved.group(1)
        elif line.startswith("*** End Patch"):
            break
        elif current is not None and line.startswith("+"):
            current["added"].append(line[1:])
    return files


def claude_events(client, event):
    """The hook event as the hooks read it (Claude Code's shape): one event for a write or an edit, several for a Codex patch that
    touches several files. Claude Code's own event is returned as it is."""
    if not isinstance(event, dict):
        return []
    roots = event.get("workspace_roots")
    base = {"session_id": event.get("session_id", event.get("conversation_id")),
            "cwd": event.get("cwd", roots[0] if isinstance(roots, list) and roots else None)}
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if client == "codex" and event.get("tool_name") == "apply_patch":
        out = []
        for file in patched_files(tool_input.get("command")):
            if file["whole"]:
                out.append(dict(event, **base, tool_name="Write", tool_input={"file_path": file["path"], "content": "\n".join(file["added"])}))
            else:
                out.append(dict(event, **base, tool_name="Edit", tool_input={"file_path": file["path"], "edits": [{"new_string": line} for line in file["added"] if line.strip()]}))
        return out
    if client == "gemini" and event.get("tool_name") in ("write_file", "replace"):
        if event["tool_name"] == "write_file":
            return [dict(event, **base, tool_name="Write", tool_input={"file_path": tool_input.get("file_path"), "content": tool_input.get("content")})]
        expected = tool_input.get("expected_replacements")
        return [dict(event, **base, tool_name="Edit", tool_input={"file_path": tool_input.get("file_path"), "new_string": tool_input.get("new_string"),
                                                                 "replace_all": isinstance(expected, int) and expected > 1})]
    if client == "cursor" and "tool_name" in event:
        file = next((tool_input[key] for key in ("file_path", "path", "target_file", "filePath") if tool_input.get(key) is not None), None)
        content = next((tool_input[key] for key in ("content", "contents", "file_text") if tool_input.get(key) is not None), None)
        return [dict(event, **base, tool_name="Write", tool_input=dict({"file_path": file}, **({"content": content} if isinstance(content, str) else {})))]
    loop_count = event.get("loop_count")
    active = event.get("stop_hook_active") is True or (isinstance(loop_count, int) and not isinstance(loop_count, bool) and loop_count > 0)
    return [dict(event, **base, stop_hook_active=active)]


def write_answer(client, note):
    """The answer to a write's hook, as the agent takes it."""
    if client == "cursor":
        return {"additional_context": note}
    return {"hookSpecificOutput": {"hookEventName": "AfterTool" if client == "gemini" else "PostToolUse", "additionalContext": note}}


def stop_answer(client, note, block):
    """The answer to the finish check, as the agent takes it: the agents other than Claude Code can only go on, so they answer only when
    the team chose to block."""
    if client == "claude":
        return {"decision": "block", "reason": note} if block else {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": note}}
    if not block:
        return None
    if client == "cursor":
        return {"followup_message": note}
    if client == "gemini":
        return {"decision": "deny", "reason": note}
    return {"decision": "block", "reason": note}


def brief_answer(client, card):
    """The answer to the session brief, as the agent takes it."""
    if client == "cursor":
        return {"additional_context": card}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": card}}
