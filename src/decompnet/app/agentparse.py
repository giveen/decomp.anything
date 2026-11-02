"""Reading what an agent CLI prints: its tool calls, its messages, and its errors.

A CLI's stdout is a stream of JSON records whose shape differs per tool (Claude Code's assistant/tool_use records,
Antigravity's events, Codex's log lines). parse_line turns one line into (kind, text) for the session view, and
clean_stderr drops the noise a CLI prints about itself, so the page shows what the agent did and not its startup banner.
A line nobody recognizes is kept as text rather than guessed at."""

from __future__ import annotations

import json
import re

from .agenttools import AGY_ALLOW, agy_settings


def _tool_name(name: str) -> str:
    for prefix in ("mcp__decomp__", "mcp_decomp_", "decomp_agent.", "decomp_agent__", "decomp__", "decomp_", "decomp."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name

def _args_brief(args) -> str:
    if not isinstance(args, dict):
        return ""
    parts = []
    for k, v in args.items():
        if k == "source":
            parts.append(f"{len(str(v).splitlines())} lines of C")
        elif isinstance(v, str):
            parts.append(v if len(v) < 40 else v[:37] + "…")
        elif v not in (None, "", {}):
            parts.append(str(v))
    return ", ".join(parts)

def _error_text(err) -> str:
    """The message in an agent's error object, whatever its shape (opencode nests it under data)."""
    if isinstance(err, dict):
        data = inner if isinstance(inner := err.get("data"), dict) else {}  # opencode nests the message under data
        msg = err.get("message") or data.get("message") or ""
        name = err.get("name") or err.get("type") or ""
        if msg and name and name not in ("UnknownError", "Error") and name not in msg:
            return f"{name}: {msg}"
        return str(msg or name or json.dumps(err)[:300])
    return str(err or "")

def _parse_agy(obj: dict, state: dict) -> list[tuple[str, str]]:
    """Antigravity CLI's stream-json: step_update events (text arrives in deltas, gathered per step) and a result."""
    out: list[tuple[str, str]] = []
    ev = obj.get("event")
    if ev == "step_update" and isinstance(obj.get("step_update"), dict):
        st = obj["step_update"]
        step, kind = st.get("step_index"), st.get("step_type")
        if kind in ("agent_response", "thinking", "planner_response"):
            texts = state.setdefault("agy_text", {})
            texts[step] = texts.get(step, "") + str(st.get("text_delta") or "")
            if st.get("state") == "DONE" and texts.get(step, "").strip():
                out.append(("think" if kind == "thinking" else "text", texts.pop(step).strip()))
        elif kind == "tool" and st.get("state") == "ACTIVE" and step not in state.setdefault("agy_tools", set()):
            state["agy_tools"].add(step)
            info = st.get("tool_info") if isinstance(st.get("tool_info"), dict) else {}
            name, params = info.get("name") or st.get("tool_name") or "", info.get("parameters")
            if name == "call_mcp_tool" and isinstance(params, dict):  # agy reaches MCP tools through one dispatcher
                name, params = params.get("ToolName") or name, params.get("Arguments")
            out.append(("tool", f"{_tool_name(name)}({_args_brief(params)})"))
    elif ev == "result" and isinstance(obj.get("result"), dict):
        res = obj["result"]
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        if usage.get("total_tokens"):
            out.append(("usage", json.dumps({"tokens": usage["total_tokens"]})))
        denied = [d for d in res.get("denied_actions") or [] if isinstance(d, dict)]
        if any(d.get("action") == "mcp" for d in denied):
            out.append(("error", f"Antigravity refused decomp.anything's tools: a background run can't ask you for permission. "
                                 f"Allow them once: add \"{AGY_ALLOW}\" to \"permissions\": {{\"allow\": [...]}} in "
                                 f"{agy_settings()} (only decomp.anything's tools; shell, files and the web keep asking)."))
        if res.get("status") not in (None, "SUCCESS"):
            out.append(("error", str(res.get("error") or f"session ended: {res.get('status')}")[:400]))
        else:
            turns = res.get("num_turns")
            out.append(("info", "session finished" + (f" ({turns} turns)" if turns else "")))
    elif ev == "error":
        out.append(("error", _error_text(obj.get("error") or obj.get("message"))[:400]))
    return out

def clean_stderr(kind: str, line: str) -> str:
    """One line of an agent's stderr as worth showing, or "" for noise."""
    line = line.strip()
    if kind == "opencode" and "level=" in line:  # opencode --print-logs: keep the error itself, drop stack noise
        # (the error is error="..." before OpenCode 2, cause="..." from 2 on, its stack trace escaped inside it)
        m = re.search(r'\b(?:error|cause)="((?:[^"\\]|\\.)*)"', line)
        return m.group(1).replace('\\"', '"').split("\\n")[0] if m and "level=ERROR" in line else ""
    if kind == "opencode" and line.startswith("at "):  # a stack frame of a crash: its message came lines before
        return ""
    if line.startswith("AGY_ERROR:"):  # agy: a structured error line
        try:
            return _error_text(json.loads(line.split(":", 1)[1]))[:300]
        except ValueError:
            return line
    return line

def parse_line(kind: str, line: str, state: dict | None = None) -> list[tuple[str, str]]:
    """(type, text) items for the live feed from one line of agent output: text, think, tool, info, error, usage.
    `state` carries what spans lines (Antigravity streams text in pieces) for one session."""
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return [("info", line[:400])]
    if not isinstance(obj, dict):
        return []
    if "event" in obj and "type" not in obj:  # Antigravity CLI
        return [(k, v) for k, v in _parse_agy(obj, {} if state is None else state) if v]
    t = obj.get("type")
    out: list[tuple[str, str]] = []
    if t == "assistant" and isinstance(obj.get("message"), dict):  # Claude Code
        for c in obj["message"].get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(("text", c["text"].strip()))
            elif c.get("type") == "thinking" and c.get("thinking", "").strip():
                out.append(("think", c["thinking"].strip()))
            elif c.get("type") == "tool_use" and c.get("name") != "ToolSearch":  # ToolSearch: Claude Code loading tools
                out.append(("tool", f"{_tool_name(c.get('name', ''))}({_args_brief(c.get('input'))})"))
    elif t == "result":  # Claude Code end of session
        if obj.get("is_error") or obj.get("status") == "error":
            out.append(("error", str(obj.get("result") or obj.get("error") or "session failed")[:400]))
        else:
            cost = obj.get("total_cost_usd")
            turns = obj.get("num_turns")
            bits = [f"{turns} turns" if turns else "", f"${cost:.2f}" if isinstance(cost, (int, float)) else ""]
            out.append(("info", "session finished" + (f" ({', '.join(b for b in bits if b)})" if any(bits) else "")))
            if isinstance(cost, (int, float)):
                out.append(("usage", json.dumps({"usd": cost})))
            stats = st if isinstance(st := obj.get("stats"), dict) else {}
            if stats.get("total_tokens"):
                out.append(("usage", json.dumps({"tokens": stats["total_tokens"]})))
    elif t in ("item.started", "item.completed") and isinstance(obj.get("item"), dict):  # Codex
        item = obj["item"]
        it = item.get("type") or item.get("item_type")
        if it == "agent_message" and t == "item.completed":
            out.append(("text", str(item.get("text", "")).strip()))
        elif it == "reasoning" and t == "item.completed" and item.get("text"):
            out.append(("think", str(item["text"]).strip()))
        elif it == "mcp_tool_call" and t == "item.started":
            out.append(("tool", f"{_tool_name(item.get('tool', ''))}({_args_brief(item.get('arguments'))})"))
        elif it == "command_execution" and t == "item.started":
            out.append(("tool", f"shell: {str(item.get('command', ''))[:80]}"))
        elif it == "error":
            out.append(("error", str(item.get("message", ""))[:400]))
    elif t == "turn.completed":
        out.append(("info", "turn finished"))
        usage = used if isinstance(used := obj.get("usage"), dict) else {}
        tokens = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int))
        if tokens:
            out.append(("usage", json.dumps({"tokens": tokens})))
    elif t in ("error", "turn.failed"):  # Codex; opencode: {"error": {"name", "data": {"message"}}}
        text = _error_text(obj.get("error") or obj.get("message"))
        if not text.startswith("Unexpected server error"):  # opencode's placeholder: its log line says what failed
            out.append(("error", text[:400]))
    elif t == "tool_use":  # opencode
        part = piece if isinstance(piece := obj.get("part"), dict) else obj
        name = part.get("tool_name") or part.get("tool") or part.get("name") or ""
        args = part.get("parameters") or (part.get("state") or {}).get("input") or part.get("input")
        out.append(("tool", f"{_tool_name(name)}({_args_brief(args)})"))
    elif t == "text" and isinstance(obj.get("part"), dict):  # opencode
        text = obj["part"].get("text", "").strip()
        if text:
            out.append(("text", text))
    elif t in ("step_finish", "step-finish") and isinstance(obj.get("part"), dict):  # opencode: cost per step
        part = obj["part"]
        if isinstance(part.get("cost"), (int, float)) and part["cost"]:
            out.append(("usage", json.dumps({"usd": part["cost"]})))
    return [(k, v) for k, v in out if v]

