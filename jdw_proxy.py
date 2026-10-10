#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JDW Fix Proxy
=============
An Anthropic-compatible (`/v1/messages`) reverse proxy that sits in front of a
broken upstream relay (api.justwoker.icu, model `claude-opus-4-8`) and repairs
the protocol violations documented in JDW_PROTOCOL_AUDIT.md.

What the upstream does wrong (and what this proxy fixes):

  1. Streaming drops all text + tool_use blocks (only thinking deltas survive).
     -> We ALWAYS call the upstream in non-stream mode (where the full content
        is intact) and SYNTHESIZE a correct SSE stream for the client.

  2. Client-supplied `tools` / `tool_choice` are discarded; the model only knows
     its own injected tools (read_tabular / system_todo_write).
     -> We inject the client's tools into the bottom of the system prompt with a
        strict JSON "tool_call" output contract, then PARSE the model's text and
        REWRITE it into real Anthropic `tool_use` blocks (stop_reason=tool_use).

  3. max_tokens ignored (never returns stop_reason=max_tokens).
     -> We truncate output to max_tokens and set stop_reason correctly.

  4. stop_sequences ignored (stop word leaks into output).
     -> We cut the text before the stop sequence and report it.

  5. ~10.4k phantom context tokens inflate usage.
     -> We normalize usage (subtract the measured baseline).

  6. thinking blocks leak into content even when not requested.
     -> Optionally stripped.

  Plus: local /v1/messages/count_tokens, /v1/models passthrough, proper
  Anthropic-shaped errors, and a bilingual (EN/RU) web dashboard at `/`.

Run:  python jdw_proxy.py
"""

import json
import os
import re
import time
import uuid
import html as _html
import base64
import hashlib
import collections
import asyncio
import threading
from collections import deque
from urllib.parse import unquote, urlparse, parse_qs
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "upstream_base_url": "https://api.justwoker.icu/v1",
    "api_key": "",
    # Optional extra keys tried automatically only if the primary key is
    # genuinely rejected (401/403) after its retries are exhausted.
    "fallback_api_keys": [],
    # --- Backup API (optional, low-cost failover) --------------------------- #
    # A SECOND, independent gateway tried AT MOST ONCE after the primary gives a
    # clear rejection (a real 4xx that is not transient) or a connection
    # failure. Timeouts and uncertain delivery are NOT replayed here (we must
    # never double-charge the user for an answer that may already be running).
    # Both gateways must speak the Anthropic /messages protocol. This lets you
    # chain several gateways: primary first, backup as a cheap safety net.
    "backup_enabled": False,
    # Leave blank to reuse the PRIMARY base URL with the backup key.
    "backup_base_url": "",
    "backup_api_key": "",
    # Leave blank to use the SAME model the client requested.
    "backup_model": "",
    "listen_host": "127.0.0.1",
    "listen_port": 8181,
    "model": "claude-opus-4-8",
    "upstream_timeout_s": 75,
    "features": {
        "tool_injection": True,
        "strict_tool_names": True,
        "flatten_tool_history": True,
        "strip_thinking": True,
        "enforce_max_tokens": True,
        "enforce_stop_sequences": True,
        # Show the SAME numbers the upstream bills on (the website figure).
        # "raw"        -> report upstream input/output verbatim (matches site).
        # "normalized" -> subtract a fixed baseline (misleads: hides real cost).
        # "estimated"  -> subtract baseline + estimate output from text length.
        "usage_mode": "raw",              # "raw" | "normalized" | "estimated"
        "usage_baseline_tokens": 10380,
        # --- Token-saving controls (reduce what is actually sent upstream) ---
        # Compact tool schemas: send a short description + field names only,
        # instead of the full JSON schema (saves thousands of tokens/request).
        "compact_tool_schemas": True,
        # Ultra-compact: one line per tool (name + fields only). Max tool saving.
        "ultra_compact_tools": True,
        # Keep only the last N messages of history (0 = unlimited / keep all).
        "max_history_messages": 30,
        # Truncate any single tool-result text longer than this many characters
        # before sending upstream (0 = no truncation). This is the single biggest
        # lever on real cost: without it, a large past read/write is replayed in
        # FULL to the upstream on EVERY later turn, so history tokens balloon.
        "max_tool_result_chars": 6000,
        # Graduated trimming: keep the last N tool-results (and tool_use args)
        # at FULL size so the model never loses context it is actively using;
        # anything OLDER than that is hard-trimmed to max_tool_result_chars.
        # This cuts repeated history cost without hurting the current step.
        # 0 = disabled (apply max_tool_result_chars uniformly to all).
        "recent_tool_results_full": 3,
        # De-duplicate repeated tool-results: if the SAME file/output is read
        # more than once, older copies are collapsed to a short marker that
        # points at the newest copy. Safe for quality (the latest copy is kept
        # intact), big win when the model re-reads the same file across turns.
        "dedup_tool_results": True,
        # Hard cap on the model's OUTPUT tokens per turn. When > 0 this OVERRIDES
        # whatever max_tokens the client sent (clamped to this value), capping
        # generation cost. 0 = OFF (honour the client's requested max_tokens).
        "max_output_tokens": 0,
        # --- Server-side image tool (executed BY the proxy, not the client) ---
        # When on, the proxy injects a fetch_image tool, fetches the image URL
        # itself, and feeds the image back to the model as a proper image block,
        # looping until the model gives a final answer. Web search / page fetch
        # have been removed; only image fetching remains.
        "web_tools_enabled": True,
        # Max server-side tool round-trips per request (safety against loops).
        "web_tools_max_iters": 4,
        # Max SIMULTANEOUS upstream requests. The relay has a limited number of
        # channels; firing many requests at once is what triggers "No available
        # channel". We gate concurrent upstream calls through a semaphore so we
        # never ask the relay for more slots than it can serve. Extra requests
        # queue briefly instead of failing. 0 = unlimited (old behaviour).
        "max_upstream_concurrency": 6,
        # One-tool-per-turn (agentic step mode). When on, the proxy keeps only
        # the FIRST tool_use block in a response and drops any extra tool calls
        # that came after it (text/thinking before the first tool is kept).
        # This forces the big "think + fire 5 tools at once" responses -- which
        # take too long upstream and trip the relay's ~100s hard timeout (524) --
        # into small, fast single steps: run one tool, return its result, let
        # the model decide the next step. Mirrors how large agent apps behave
        # and sharply reduces 524s on long chains. 0/false = old multi-tool.
        "one_tool_per_turn": True,
    },
    "ui_lang": "en",
}

_config_lock = threading.RLock()


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
            # shallow+features merge
            for k, v in disk.items():
                if k == "features" and isinstance(v, dict):
                    cfg["features"].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[config] failed to read {CONFIG_PATH}: {e}")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    with _config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)


CONFIG = load_config()


# --------------------------------------------------------------------------- #
# Shared upstream HTTP client + concurrency gate
# --------------------------------------------------------------------------- #
# A SINGLE AsyncClient is reused for every upstream call so TCP + TLS
# connections are pooled and kept alive instead of being torn down and rebuilt
# on every request (much faster, far less load on the relay).
_UP_CLIENT: Optional[httpx.AsyncClient] = None
# Gate on simultaneous upstream calls. Built lazily from config so the dashboard
# can change the limit without a restart (we rebuild it when the value changes).
_UP_SEM: Optional[asyncio.Semaphore] = None
_UP_SEM_LIMIT: int = -1
# uvicorn.Server instance, stored so /admin/shutdown can stop it gracefully
# without needing to find the console window / Python process by hand.
_SERVER: Any = None


def get_upstream_client() -> httpx.AsyncClient:
    """Return the shared pooled AsyncClient, creating it on first use."""
    global _UP_CLIENT
    if _UP_CLIENT is None or _UP_CLIENT.is_closed:
        timeout = httpx.Timeout(float(CONFIG.get("upstream_timeout_s", 300)))
        limits = httpx.Limits(max_connections=50,
                              max_keepalive_connections=20,
                              keepalive_expiry=30.0)
        _UP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)
    return _UP_CLIENT


def get_upstream_sem() -> Optional[asyncio.Semaphore]:
    """Return the concurrency gate, rebuilding it if the configured limit
    changed. Returns None when concurrency is unlimited (limit <= 0)."""
    global _UP_SEM, _UP_SEM_LIMIT
    limit = int(CONFIG.get("features", {}).get("max_upstream_concurrency", 0) or 0)
    if limit <= 0:
        _UP_SEM = None
        _UP_SEM_LIMIT = 0
        return None
    if _UP_SEM is None or _UP_SEM_LIMIT != limit:
        _UP_SEM = asyncio.Semaphore(limit)
        _UP_SEM_LIMIT = limit
    return _UP_SEM


# Adaptive capacity cooldown. When the relay says "no available channel" we
# remember the moment and make the next upstream calls wait a short, growing
# interval instead of all retrying at once -- which is what turns a brief
# shortage into a storm. The window decays automatically once requests succeed.
_CAP_COOLDOWN_UNTIL: float = 0.0
_CAP_STRIKES: int = 0
_CAP_LOCK = threading.Lock()


def note_capacity_error() -> None:
    """Record a 'no available channel' hit and extend the cooldown window."""
    global _CAP_COOLDOWN_UNTIL, _CAP_STRIKES
    with _CAP_LOCK:
        _CAP_STRIKES = min(_CAP_STRIKES + 1, 6)
        # 1.5s, 3s, 4.5s ... capped at 9s
        wait = min(1.5 * _CAP_STRIKES, 9.0)
        _CAP_COOLDOWN_UNTIL = time.time() + wait


def note_capacity_ok() -> None:
    """A successful upstream call clears the capacity pressure."""
    global _CAP_COOLDOWN_UNTIL, _CAP_STRIKES
    with _CAP_LOCK:
        _CAP_STRIKES = 0
        _CAP_COOLDOWN_UNTIL = 0.0


async def _wait_capacity_slot() -> None:
    """If the relay recently ran out of channels, hold this request briefly so
    we give it room to recover rather than piling on more load."""
    with _CAP_LOCK:
        remaining = _CAP_COOLDOWN_UNTIL - time.time()
    if remaining > 0:
        log_event({"capacity_wait_s": round(remaining, 2),
                   "note": "relay out of channels -> throttling before upstream"})
        await asyncio.sleep(min(remaining, 9.0))


# --------------------------------------------------------------------------- #
# In-memory request log (for the dashboard)
# --------------------------------------------------------------------------- #

LOG: "deque[Dict[str, Any]]" = deque(maxlen=200)
_log_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Duplicate-request safeguard (in-flight coalescing)
# --------------------------------------------------------------------------- #
# Clients (and our own retry/keepalive paths) can submit the SAME request body
# twice in a short window -- e.g. a double-click, a client-side retry, or a
# dropped-then-reopened stream. Each extra submission costs real upstream tokens
# for an answer we are already generating. To prevent that, we key each upstream
# call by a hash of its payload: if an identical request is ALREADY in flight,
# the second caller awaits the SAME result (a single upstream round-trip, one
# token charge) instead of firing a duplicate. Entries are removed as soon as
# the original call completes, so this only coalesces genuinely-concurrent
# duplicates and never caches/serves stale answers.
_inflight: Dict[str, "asyncio.Future"] = {}
_inflight_lock = asyncio.Lock()


def _payload_fingerprint(payload: Dict[str, Any]) -> str:
    """Stable hash of the parts of a payload that determine the answer.
    Ignores volatile/diagnostic keys so true duplicates collide."""
    import hashlib
    try:
        relevant = {k: payload.get(k) for k in
                    ("model", "messages", "system", "tools", "tool_choice",
                     "max_tokens", "temperature", "top_p", "top_k",
                     "stop_sequences", "thinking")}
        blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except Exception:
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def log_event(entry: Dict[str, Any]) -> None:
    entry["ts"] = time.time()
    with _log_lock:
        LOG.appendleft(entry)


def _fix_invalid_json_escapes(raw: str) -> str:
    r"""Repair stray single backslashes in JSON strings (e.g. \( \. \s \w)
    that cause json.loads to fail with 'Invalid \escape'."""
    pattern = r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}|(.|\n))'
    def repl(m):
        invalid_char = m.group(1)
        if invalid_char is not None:
            return "\\\\" + invalid_char
        return m.group(0)
    return re.sub(pattern, repl, raw)


def _loads_tool_json(raw: str) -> Optional[Dict[str, Any]]:
    r"""Parse the JSON inside a tool_call fence as robustly as possible.

    The model very often emits tool arguments (edit old_string/new_string,
    write content, regex patterns, etc.) that contain *literal* newlines, tabs,
    or unescaped backslashes (like \( or \.) INSIDE JSON string values. Strict
    json.loads rejects those with 'Invalid control character' or 'Invalid \escape',
    which used to make the whole tool_call fall back to being shown as a plain
    ```code``` block that never executed. We try progressively more lenient
    strategies:

      1. strict json.loads (fast path, already-valid JSON)
      2. json.loads(..., strict=False) -> allows literal control chars in strings
      3. a pass that escapes stray control chars & fixes invalid \escapes, then retries
      4. brace/bracket balancing fallback
    """
    if not raw:
        return None
    # 1) strict
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 2) lenient: permit literal control characters inside strings
    try:
        obj = json.loads(raw, strict=False)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 3) escape stray control chars (newline/tab/cr) + repair unescaped backslashes
    fixed = (raw.replace("\r\n", "\\n")
                .replace("\n", "\\n")
                .replace("\r", "\\n")
                .replace("\t", "\\t"))
    fixed = _fix_invalid_json_escapes(fixed)
    try:
        obj = json.loads(fixed, strict=False)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 4) brace/bracket balancing: the model sometimes appends EXTRA closing
    #    brackets ('..."}]}]}') or leaves some open.
    for candidate in (raw, fixed):
        extracted = _extract_balanced_json(candidate)
        if extracted is not None:
            try:
                obj = json.loads(extracted, strict=False)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def _extract_balanced_json(s: str) -> Optional[str]:
    """Return a cleaned, balanced {...} object extracted from `s`.

    Repairs two common model mistakes:
      * EXTRA / mismatched closing brackets (e.g. '..."}]}]}') -> stray
        closers that don't match the current open bracket are DROPPED, so the
        rebuilt object is valid JSON instead of over-closed junk.
      * MISSING closers -> appended in the right order at the end.
    String contents and escapes are respected so braces inside strings never
    affect the depth count.
    """
    start = s.find("{")
    if start < 0:
        return None
    pairs = {"}": "{", "]": "["}
    buf: List[str] = []
    stack: List[str] = []
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "{[":
            stack.append(ch)
            buf.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
                buf.append(ch)
                if not stack:
                    # root object fully + correctly closed -> done
                    return "".join(buf)
            else:
                # stray / mismatched closer -> drop it (the model's extra
                # ']' or '}' that caused the whole call to fail to parse)
                continue
        else:
            buf.append(ch)
    # never closed -> append the missing closers in reverse order
    if stack:
        for c in reversed(stack):
            buf.append("}" if c == "{" else "]")
        return "".join(buf)
    return None


# --------------------------------------------------------------------------- #
# Tool-injection: build the system-prompt addendum + parse tool_call blocks
# --------------------------------------------------------------------------- #

# Match a fenced block with ANY (or no) language label -- the model often
# tags a tool call with the SHELL language it is invoking (```powershell,
# ```bash, ```sh, ```pwsh) or a bare ``` fence, not just ```tool_call /
# ```json. We still only ACCEPT a block as a tool call later if its JSON
# body actually carries a "name" (+ optional "input"), so a real code block
# is never misread as a call.
TOOL_CALL_FENCE_RE = re.compile(
    r"```[A-Za-z0-9_+.\-]*[ \t]*\r?\n(\{.*?\})[ \t]*\r?\n?```",
    re.DOTALL,
)
# Fallback for a BARE JSON tool call emitted with no fence at all: a top-level
# object that contains a "name" key. Used only when no fenced call was found.
# We locate candidates by scanning for a '{' that starts a brace-balanced span
# (string-aware, so braces inside "command": "... { } ..." don't confuse it).
_NAME_HINT_RE = re.compile(r"\{\s*\"name\"\s*:")


def _iter_balanced_json_objects(text: str):
    """Yield (start, end, substring) for every brace-balanced {...} span in
    `text` that begins with a {"name": ... hint, honouring JSON string quoting
    and escapes so braces inside string values are ignored."""
    for hint in _NAME_HINT_RE.finditer(text):
        start = hint.start()
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield start, i + 1, text[start:i + 1]
                    break

# The underlying model was trained on Anthropic's NATIVE XML tool-call syntax
# and frequently falls back to it (optionally with an `antml:` namespace),
# ignoring our ```tool_call fence instruction. It also sometimes MIXES the two
# (a JSON fence followed by stray </parameter></invoke> tags). We parse this
# form too so the tool ALWAYS executes instead of leaking as a code block.
INVOKE_XML_RE = re.compile(
    r"<(?:antml:)?invoke\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</(?:antml:)?invoke>",
    re.DOTALL,
)
PARAM_XML_RE = re.compile(
    r"<(?:antml:)?parameter\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</(?:antml:)?parameter>",
    re.DOTALL,
)
# Opening-tag matchers used by the tolerant parser. We deliberately do NOT
# require closing tags, because the model very often OMITS the closing
#  on the last parameter (which used to make that whole parameter
# -- e.g. file_path -- vanish and the tool fail with "missing required
# property").
INVOKE_NAME_RE = re.compile(r"<(?:antml:)?invoke\s+name\s*=\s*\"([^\"]+)\"\s*>")
PARAM_OPEN_RE = re.compile(r"<(?:antml:)?parameter\s+name\s*=\s*\"([^\"]+)\"\s*>")


def _coerce_param_value(raw: str) -> Any:
    """Convert an XML <parameter> text body into a Python value. Parameter
    bodies are always text; we try JSON first (so numbers/bools/arrays/objects
    arrive typed) and fall back to the raw string (the common case for code in
    old_string/new_string/content)."""
    s = raw.strip()
    if s and (s[0] in "[{" or s in ("true", "false", "null")
              or re.fullmatch(r"-?\d+(?:\.\d+)?", s)):
        try:
            return json.loads(s)
        except Exception:
            pass
    return raw


def _parse_invoke_xml(segment: str) -> Optional[Dict[str, Any]]:
    """Parse one <invoke name="X">...<parameter...>...</invoke> segment into
    {"name": X, "input": {...}}. Returns None if no name found."""
    nm = INVOKE_NAME_RE.search(segment)
    if not nm:
        return None
    name = nm.group(1).strip()
    body = segment[nm.end():]
    close = re.search(r"</(?:antml:)?invoke>", body)
    if close:
        body = body[:close.start()]
    inp: Dict[str, Any] = {}
    opens = list(PARAM_OPEN_RE.finditer(body))
    for i, pm in enumerate(opens):
        key = pm.group(1).strip()
        val_start = pm.end()
        val_end = opens[i + 1].start() if i + 1 < len(opens) else len(body)
        val = body[val_start:val_end]
        val = re.sub(r"</(?:antml:)?parameter>\s*$", "", val)
        val = re.sub(r"</(?:antml:)?invoke>\s*$", "", val)
        inp[key] = _coerce_param_value(val)
    return {"name": name, "input": inp}

TOOL_PREAMBLE_EN = (
    "## IMPORTANT — Tool access override\n"
    "The hosting provider misconfigured native tool support on this endpoint, so "
    "the normal Anthropic `tools` mechanism is not delivered to you. A compatibility "
    "proxy restores it for you here. You DO have access to the tools listed below, "
    "and you MUST use them when the task requires it.\n\n"
    "To call a tool, emit a fenced code block tagged `tool_call` containing a single "
    "JSON object with exactly two keys: `name` (the tool name) and `input` (the "
    "arguments object matching that tool's JSON schema). Example:\n\n"
    "```tool_call\n"
    "{\"name\": \"read_file\", \"input\": {\"path\": \"src/app.ts\"}}\n"
    "```\n\n"
    "Rules:\n"
    "- Emit the tool_call block and then STOP; do not predict the tool's result.\n"
    "- You may write a short sentence before the block explaining what you are about to do.\n"
    "- Use ONE tool_call block per turn unless the task clearly needs several.\n"
    "- The `input` must be valid JSON and conform to the tool's schema.\n"
    "- Call tools ONLY by a name that appears in the list below. Never invent a\n"
    "  tool name or call one that is not listed; if a capability is not listed,\n"
    "  solve the task without a tool.\n\n"
    "If the user attaches images (e.g. a screenshot), you can see them directly; "
    "describe or act on their visual content as needed.\n\n"
    "If an image tool (e.g. `fetch_image`) is listed below, you CAN fetch an image "
    "by URL and see it directly. Use it whenever you need to look at an image at a "
    "specific URL.\n\n"
    "### Available tools\n"
)

TOOL_PREAMBLE_FORCED_EN = (
    "\nThe caller REQUIRES you to call the tool named `{name}` on this turn. "
    "Respond with exactly one `tool_call` block for `{name}` and nothing else.\n"
)


def _compact_schema_fields(schema: Dict[str, Any], depth: int = 1) -> str:
    """Render a tool schema as a short one-line field summary instead of the
    full JSON schema. Keeps names + types + required flags, drops verbose
    descriptions -> massive token savings.

    `depth` controls how far nested object/array<object> fields are expanded
    inline (depth=1 expands one level, so array<object> shows its sub-fields
    like questions*:array<{id*,question*,options}>). Expanding one level is
    what keeps tool CALLS correct under ultra-compact: the model can see the
    shape of nested arguments instead of guessing.
    """
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or [])
    parts = []
    for pname, pdef in props.items():
        ptype = ""
        sub = ""
        if isinstance(pdef, dict):
            ptype = pdef.get("type") or ""
            if ptype == "array" and isinstance(pdef.get("items"), dict):
                items = pdef["items"]
                it = items.get("type") or ""
                if it == "object" and depth > 0:
                    inner = _compact_schema_fields(items, depth - 1)
                    sub = f"array<{{{inner}}}>" if inner else "array<object>"
                else:
                    sub = f"array<{it}>" if it else "array"
                ptype = sub
            elif ptype == "object" and depth > 0:
                inner = _compact_schema_fields(pdef, depth - 1)
                ptype = f"object{{{inner}}}" if inner else "object"
        star = "*" if pname in required else ""
        parts.append(f"{pname}{star}:{ptype}" if ptype else f"{pname}{star}")
    return ", ".join(parts)


def _tool_capability_hint(name: str, desc: str = "") -> str:
    """Return a SHORT, concrete capability note tailored to THIS tool, keyed off
    its name/description. Tells the model, in plain terms, the useful things it
    can actually do with the tool (e.g. 'with me you can push to GitHub') instead
    of a generic blurb. Empty string if we have no specific hint."""
    n = (name or "").lower()
    d = (desc or "").lower()
    def has(*subs: str) -> bool:
        return any(s in n or s in d for s in subs)
    # Shell / command runners
    if has("pwsh", "powershell", "bash", "shell", "terminal", "command", "exec", "run_command"):
        return ("With me you run real commands on this machine. You CAN: `git add/commit/push` "
                "to upload a project to GitHub, install packages, move/zip files, start servers. "
                "Put the WHOLE command (even multi-line git here-strings) in `input` and CALL me — "
                "never just print the command as a code block.")
    # Image fetch / vision by URL
    if has("fetch_image", "image", "screenshot", "vision", "img"):
        return ("With me you download an image from a URL and SEE it directly, so you can read "
                "text in it, describe it, or compare screenshots.")
    # URL / web fetch
    if has("fetch_url", "web_fetch", "open_url", "browse", "http_get"):
        return ("With me you open a URL and read its page content as text — use me to pull docs, "
                "API responses, or a raw file off the web.")
    # Web search
    if has("web_search", "search"):
        return ("With me you search the web for current info and get back result links/snippets "
                "to fetch next.")
    # Write / create files
    if has("write", "create_file", "new_file"):
        return ("With me you create or fully overwrite a file on disk — pass the full final "
                "content, not a diff.")
    # Edit files
    if has("edit", "replace", "patch", "apply_diff"):
        return ("With me you edit an existing file by replacing exact text — read it first so "
                "your old-text match is precise.")
    # Read files
    if has("read", "cat", "open_file", "view"):
        return ("With me you read a file's exact contents from disk before changing or quoting it.")
    # Search in files
    if has("grep", "ripgrep", "find_in"):
        return ("With me you search file CONTENTS by regex to locate code/text fast.")
    # Glob / find files
    if has("glob", "find", "list_files"):
        return ("With me you find files by path pattern (e.g. **/*.py).")
    return ""


def build_tool_system_block(tools: List[Dict[str, Any]],
                            tool_choice: Optional[Dict[str, Any]],
                            compact: bool = False,
                            ultra: bool = False,
                            one_tool: bool = False) -> str:
    lines = [TOOL_PREAMBLE_EN]
    for t in tools:
        name = t.get("name", "?")
        desc = t.get("description", "") or ""
        schema = t.get("input_schema") or t.get("inputSchema") or {}
        if ultra:
            # Ultra-compact: one line per tool, name + field names only.
            fields = _compact_schema_fields(schema)
            short = desc.strip().split("\n", 1)[0][:80]
            line = f"- `{name}`"
            if short:
                line += f": {short}"
            if fields:
                line += f"  [{fields}]"
            hint = _tool_capability_hint(name, desc)
            if hint:
                line += f"\n    → {hint}"
            lines.append(line + "\n")
        elif compact:
            # short description (first line / first ~200 chars) + field summary
            short = desc.strip().split("\n", 1)[0][:200]
            fields = _compact_schema_fields(schema)
            lines.append(f"\n#### `{name}`\n{short}\n")
            hint = _tool_capability_hint(name, desc)
            if hint:
                lines.append(f"What you can do with it: {hint}\n")
            if fields:
                lines.append(f"Params ( * = required ): {fields}\n")
        else:
            lines.append(f"\n#### `{name}`\n{desc}\n")
            hint = _tool_capability_hint(name, desc)
            if hint:
                lines.append(f"What you can do with it: {hint}\n")
            try:
                schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
            except Exception:
                schema_str = str(schema)
            lines.append(f"Input JSON schema:\n```json\n{schema_str}\n```\n")

    # Make the closed set of valid names explicit so the model can't invent one.
    valid_names = [t.get("name") for t in tools if t.get("name")]
    if valid_names:
        lines.append(
            "\n### Valid tool names (the ONLY names you may call)\n"
            + ", ".join(f"`{n}`" for n in valid_names)
            + "\n"
        )

    block = "".join(lines)

    # SINGLE-STEP MODE. When the proxy runs one-tool-per-turn, tell the model
    # EXPLICITLY to emit exactly one tool_call and stop. This is what actually
    # prevents the relay's ~100s 524 timeout: a response that fires 5 tools at
    # once takes far too long to GENERATE upstream, so trimming it after the
    # fact is too late -- the generation already timed out. Instructing the
    # model to stop after the first call keeps each turn short and fast.
    if one_tool:
        block += (
            "\n### Single-step execution (REQUIRED)\n"
            "Emit EXACTLY ONE `tool_call` block per turn, then STOP immediately "
            "and wait for its result before deciding the next step. Do NOT plan "
            "or emit several tool calls in one reply, even if you can foresee the "
            "later steps -- issue only the single next action and continue after "
            "you see its result.\n"
        )

    if tool_choice:
        ctype = tool_choice.get("type")
        if ctype == "tool" and tool_choice.get("name"):
            block += TOOL_PREAMBLE_FORCED_EN.format(name=tool_choice["name"])
        elif ctype == "any":
            block += ("\nThe caller requires you to call one of the available tools "
                      "on this turn. Respond with a `tool_call` block.\n")
    return block


def extract_system_text(system: Any) -> str:
    """Anthropic `system` may be a string or a list of blocks."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return str(system)


# --------------------------------------------------------------------------- #
# History flattening: turn tool_use / tool_result blocks into text the upstream
# model can actually read (since it never saw the real tool protocol).
# --------------------------------------------------------------------------- #

def flatten_content_blocks(content: Any, role: str,
                           max_tool_result_chars: int = 0) -> str:
    """Convert a message's content (string or block list) into plain text,
    rendering tool_use / tool_result into the JSON contract format.

    Tool-result text longer than max_tool_result_chars (when > 0) is truncated
    with a short marker, to cap upstream token cost from huge tool outputs."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: List[str] = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        btype = b.get("type")
        if btype == "text":
            parts.append(b.get("text", ""))
        elif btype == "thinking":
            # don't feed stale thinking back
            continue
        elif btype == "tool_use":
            tin = b.get("input", {})
            # Cap huge tool_use ARGUMENTS in history too. A past write/edit whose
            # content/old_string/new_string holds a whole large file would
            # otherwise be replayed in FULL to the upstream on every subsequent
            # turn -- silently inflating input tokens (we saw 15k "measured" vs
            # 69k actual) until the request got heavy enough that the relay
            # stalled ~75s and dropped the connection ("Upstream connection
            # failed"). These args describe an action the model ALREADY took;
            # the upstream only needs to see that it happened, not re-read the
            # entire payload. Truncating here is the real fix for "it dies when
            # reading/writing large files".
            if (max_tool_result_chars and isinstance(tin, dict)):
                _tin2 = {}
                for _k, _v in tin.items():
                    if (isinstance(_v, str) and len(_v) > max_tool_result_chars):
                        _omit = len(_v) - max_tool_result_chars
                        _tin2[_k] = (_v[:max_tool_result_chars]
                                     + f"\n...[truncated {_omit} chars to save tokens]")
                    else:
                        _tin2[_k] = _v
                tin = _tin2
            payload = {"name": b.get("name"), "input": tin}
            parts.append(
                "```tool_call\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
            )
        elif btype == "tool_result":
            tcid = b.get("tool_use_id", "")
            inner = b.get("content", "")
            if isinstance(inner, list):
                texts = []
                for ib in inner:
                    if isinstance(ib, dict):
                        if ib.get("type") == "text":
                            texts.append(ib.get("text", ""))
                        elif ib.get("type") == "image":
                            texts.append("[image]")
                    else:
                        texts.append(str(ib))
                inner = "\n".join(texts)
            is_err = b.get("is_error")
            tag = "TOOL ERROR" if is_err else "TOOL RESULT"
            # Cap huge tool outputs to save upstream tokens.
            if (max_tool_result_chars and isinstance(inner, str)
                    and len(inner) > max_tool_result_chars):
                keep = max_tool_result_chars
                omitted = len(inner) - keep
                inner = (inner[:keep]
                         + f"\n...[truncated {omitted} chars to save tokens]")
            parts.append(f"[{tag} for {tcid}]\n{inner}")
        elif btype == "image":
            # keep images as proper blocks elsewhere; here mark presence
            parts.append("[image]")
        else:
            parts.append(json.dumps(b, ensure_ascii=False))
    return "\n\n".join(p for p in parts if p != "")


def message_has_image(content: Any) -> bool:
    """True if the message carries an image either as a TOP-LEVEL block or
    NESTED inside a tool_result's content list (e.g. a screenshot returned by
    a client tool). The nested case matters because such images were otherwise
    silently flattened to the literal text '[image]' -- so the model lost the
    picture AND, if ever kept raw, their base64 would bypass size handling."""
    if not isinstance(content, list):
        return False
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "image":
            return True
        if b.get("type") == "tool_result":
            inner = b.get("content")
            if isinstance(inner, list) and any(
                isinstance(ib, dict) and ib.get("type") == "image" for ib in inner
            ):
                return True
    return False


# Classify a tool name into a coarse CATEGORY so the diagnostic log groups
# tools by what they DO (read / write / execute / search / network / image /
# task / mcp / other). Matching is substring-based and case-insensitive so it
# works for client tools, server tools, and MCP-prefixed names alike.
_TOOL_CATEGORY_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("image",   ("fetch_image", "screenshot", "image", "vision", "read_image")),
    ("search",  ("web_search", "websearch", "search", "grep", "glob", "find",
                 "codebase", "lookup")),
    ("network", ("web_fetch", "webfetch", "fetch", "http", "url", "curl",
                 "download", "browser", "request")),
    ("execute", ("pwsh", "bash", "shell", "powershell", "exec", "run",
                 "command", "terminal", "process", "python", "node")),
    # task BEFORE write, so 'todo_write' / 'update_goal' classify as task
    # rather than matching the 'write' / 'update' keyword.
    ("task",    ("todo", "task", "plan", "goal", "subagent", "agent",
                 "workflow", "dispatch", "delegate")),
    ("write",   ("write", "edit", "create", "update", "apply_patch", "patch",
                 "replace", "insert", "delete", "remove", "mkdir", "move",
                 "rename", "save")),
    ("read",    ("read", "cat", "open", "view", "get", "list", "ls", "stat",
                 "show", "load", "tabular", "csv", "excel")),
]


def classify_tool(name: str) -> str:
    """Return a coarse category for a tool name (never raises)."""
    if not name:
        return "other"
    low = str(name).lower()
    # MCP tools usually look like 'mcp__server__action' -> tag as mcp but still
    # try to refine by the action keyword after the last separator.
    base = low
    if "__" in low:
        base = low.split("__")[-1]
    for cat, needles in _TOOL_CATEGORY_RULES:
        for nd in needles:
            if nd in base or nd in low:
                return cat
    return "mcp" if low.startswith("mcp") else "other"


def _tool_payload_sizes(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Diagnostic: walk the OUTBOUND message history and measure how many bytes
    each individual tool contributes (its tool_use arguments and its matching
    tool_result output). This is how we answer "which tool is making the request
    heavy / stalling the relay". Returns a list of per-tool records sorted
    biggest-first so the caller can log the top offenders."""
    # Map tool_use id -> tool name so we can attribute a tool_result back to the
    # tool that produced it (tool_result only carries the id, not the name).
    id_to_name: Dict[str, str] = {}
    recs: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "tool_use":
                name = b.get("name") or "?"
                tid = b.get("id") or ""
                if tid:
                    id_to_name[tid] = name
                try:
                    sz = len(json.dumps(b.get("input", {}), ensure_ascii=False))
                except Exception:
                    sz = len(str(b.get("input", "")))
                recs.append({"tool": name, "category": classify_tool(name),
                             "kind": "input", "bytes": sz})
            elif bt == "tool_result":
                tid = b.get("tool_use_id", "")
                name = id_to_name.get(tid, "?")
                inner = b.get("content", "")
                if isinstance(inner, list):
                    tot = 0
                    for ib in inner:
                        if isinstance(ib, dict):
                            if ib.get("type") == "text":
                                tot += len(ib.get("text", "") or "")
                            elif ib.get("type") == "image":
                                src = ib.get("source", {}) or {}
                                tot += len(str(src.get("data", "")))
                        else:
                            tot += len(str(ib))
                    sz = tot
                else:
                    sz = len(str(inner))
                recs.append({"tool": name, "category": classify_tool(name),
                             "kind": "result", "bytes": sz})
    recs.sort(key=lambda r: r["bytes"], reverse=True)
    return recs


# --------------------------------------------------------------------------- #
# Automatic image downsizing / recompression.
#
# A single large screenshot can cost ~30k input tokens and add 20-40s of
# latency. We transparently shrink any inbound base64 image whose longest side
# exceeds IMG_MAX_DIM, and re-encode it (JPEG for opaque images, PNG kept for
# images with transparency) so the upstream receives a much smaller payload.
# Only applied when it actually REDUCES the byte size; otherwise the original
# block is left untouched. Pillow is optional -- if missing we no-op.
# --------------------------------------------------------------------------- #
try:
    from PIL import Image as _PILImage  # type: ignore
    _PIL_OK = True
except Exception:
    _PIL_OK = False

IMG_MAX_DIM = 1280          # longest side after downscale
IMG_JPEG_QUALITY = 85       # recompression quality for opaque images
IMG_MIN_BYTES = 40 * 1024   # skip images already smaller than this

# fetch_image network guards (independent of the upstream model timeout):
# a slow/hanging image URL must NOT be able to stall the whole request for
# the full upstream_timeout_s (and up to web_tools_max_iters times).
IMG_FETCH_CONNECT_S = 5.0   # TCP/TLS connect budget for an image URL
IMG_FETCH_READ_S = 12.0     # read budget for an image URL
IMG_FETCH_TOTAL_S = 15.0    # hard overall ceiling per fetch_image call
IMG_FETCH_MAX_BYTES = 8 * 1024 * 1024  # reject images larger than 8 MB

# Cache of already-compressed images, keyed by a hash of the ORIGINAL base64
# payload. The same screenshot sits in the history and would otherwise be
# re-encoded (and re-logged) on EVERY subsequent turn -- wasted CPU and noisy
# logs. We cache the finished block so repeat turns are a dict lookup and the
# 'image_compressed' event is emitted exactly once per distinct image.
_IMG_CACHE: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
_IMG_CACHE_MAX = 64  # cap entries; evict oldest (LRU-ish)


def _compress_image_block(block: Dict[str, Any],
                          log: bool = True) -> Dict[str, Any]:
    """Return a possibly-smaller copy of an Anthropic base64 image block.
    Falls back to the original block on any problem.

    ``log`` controls whether a successful compression emits an
    'image_compressed' event. History images (replayed on every turn) are
    compressed SILENTLY so the live log only shows a compression when the
    user actually sends a new image in the current turn."""
    if not _PIL_OK:
        return block
    try:
        src = block.get("source") or {}
        if src.get("type") != "base64":
            return block
        raw_b64 = src.get("data") or ""
        # Serve from cache if we've already processed this exact image. This
        # both skips the re-encode and prevents duplicate log spam.
        cache_key = hashlib.sha1(raw_b64.encode("ascii", "ignore")).hexdigest()
        cached = _IMG_CACHE.get(cache_key)
        if cached is not None:
            _IMG_CACHE.move_to_end(cache_key)  # mark as recently used
            return cached
        orig_bytes = base64.b64decode(raw_b64)
        if len(orig_bytes) < IMG_MIN_BYTES:
            _IMG_CACHE[cache_key] = block  # cache the no-op too
            _img_cache_trim()
            return block
        import io as _io
        im = _PILImage.open(_io.BytesIO(orig_bytes))
        im.load()
        w, h = im.size
        longest = max(w, h)
        scale = IMG_MAX_DIM / float(longest) if longest > IMG_MAX_DIM else 1.0
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           _PILImage.LANCZOS)
        # Does the image declare an alpha channel?
        declares_alpha = im.mode in ("RGBA", "LA") or (
            im.mode == "P" and "transparency" in im.info)
        # CRITICAL: most screenshots arrive as RGBA but the alpha channel is
        # fully opaque (every pixel == 255) -- i.e. the transparency is fake.
        # Keeping those as PNG only saves ~15%; re-encoding them as JPEG saves
        # 70-85%. So only treat an image as "really transparent" when its alpha
        # channel actually contains a non-opaque pixel.
        has_real_alpha = False
        if declares_alpha:
            try:
                alpha = im.convert("RGBA").getchannel("A")
                lo, _hi = alpha.getextrema()
                has_real_alpha = lo < 255
            except Exception:
                has_real_alpha = True  # be safe -> preserve as PNG
        if has_real_alpha:
            # Genuine transparency: a JPEG would turn it into a black/garbled
            # background, so keep PNG (optimized) to stay lossless.
            buf = _io.BytesIO()
            im.convert("RGBA").save(buf, format="PNG", optimize=True)
            new_bytes = buf.getvalue()
            new_media = "image/png"
        else:
            # Opaque image: there is NO single winner. Photo-like screenshots
            # (lots of detail) compress far smaller as JPEG, but flat UI shots
            # (solid panels + crisp text) are actually smaller as PNG -- and a
            # blind JPEG can be ~2x LARGER. So encode BOTH and keep whichever is
            # smaller. This guarantees we never make the payload worse.
            rgb = im.convert("RGB")
            jbuf = _io.BytesIO()
            rgb.save(jbuf, format="JPEG", quality=IMG_JPEG_QUALITY,
                     optimize=True, progressive=True)
            jpeg_bytes = jbuf.getvalue()
            pbuf = _io.BytesIO()
            rgb.save(pbuf, format="PNG", optimize=True)
            png_bytes = pbuf.getvalue()
            if len(jpeg_bytes) <= len(png_bytes):
                new_bytes, new_media = jpeg_bytes, "image/jpeg"
            else:
                new_bytes, new_media = png_bytes, "image/png"
        if len(new_bytes) >= len(orig_bytes):
            _IMG_CACHE[cache_key] = block  # cache the no-op so we don't retry
            _img_cache_trim()
            return block  # recompression didn't help -> keep original
        if log:
            log_event({"image_compressed": True,
                       "orig_bytes": len(orig_bytes), "new_bytes": len(new_bytes),
                       "orig_dim": [w, h], "new_dim": list(im.size),
                       "saved_pct": round(100 * (1 - len(new_bytes) / len(orig_bytes)), 1)})
        result = {"type": "image",
                  "source": {"type": "base64", "media_type": new_media,
                             "data": base64.b64encode(new_bytes).decode("ascii")}}
        _IMG_CACHE[cache_key] = result
        _img_cache_trim()
        return result
    except Exception as e:
        log_event({"image_compress_error": str(e)[:200]})
        return block


def _img_cache_trim() -> None:
    """Evict oldest entries so the image cache stays bounded."""
    while len(_IMG_CACHE) > _IMG_CACHE_MAX:
        _IMG_CACHE.popitem(last=False)


def _tool_result_text(b: Dict[str, Any]) -> str:
    """Best-effort plain text of a tool_result block (for dedup signatures)."""
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for ib in c:
            if isinstance(ib, dict) and ib.get("type") == "text":
                parts.append(ib.get("text", ""))
            elif isinstance(ib, dict) and ib.get("type") == "image":
                return ""  # never dedup image-bearing results
            else:
                parts.append(str(ib))
        return "\n".join(parts)
    return ""


def dedup_tool_results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse OLDER duplicate tool-results to a short pointer marker.

    If the exact same tool-result text appears more than once (e.g. the model
    re-reads the same file across turns), every copy EXCEPT the most recent is
    replaced by a one-line marker. The latest copy is kept intact so no live
    context is lost. Image-bearing results are never touched.
    Mutates copies, not the caller's blocks.
    """
    # Record the LAST index at which each signature occurs.
    last_idx: Dict[str, Any] = {}
    for mi, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for bi, b in enumerate(content):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                txt = _tool_result_text(b)
                if len(txt) >= 200:  # only worth deduping sizeable results
                    last_idx[txt] = (mi, bi)
    if not last_idx:
        return messages
    seen_counts: Dict[str, int] = {}
    for v in last_idx:
        seen_counts[v] = 0
    # Count total occurrences to know which signatures actually repeat.
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                txt = _tool_result_text(b)
                if txt in seen_counts:
                    seen_counts[txt] += 1
    out: List[Dict[str, Any]] = []
    for mi, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_content = []
        changed = False
        for bi, b in enumerate(content):
            if (isinstance(b, dict) and b.get("type") == "tool_result"):
                txt = _tool_result_text(b)
                if (txt in seen_counts and seen_counts[txt] > 1
                        and last_idx.get(txt) != (mi, bi)):
                    nb = dict(b)
                    nb["content"] = ("[duplicate tool_result collapsed - "
                                     "identical output shown in a later turn]")
                    new_content.append(nb)
                    changed = True
                    continue
            new_content.append(b)
        if changed:
            nm = dict(m)
            nm["content"] = new_content
            out.append(nm)
        else:
            out.append(m)
    return out


def flatten_messages(messages: List[Dict[str, Any]],
                     flatten_tools: bool,
                     max_tool_result_chars: int = 0,
                     recent_full: int = 0) -> List[Dict[str, Any]]:
    """Produce an upstream-friendly messages array.

    If a message contains images we KEEP the structured block list (upstream
    supports image blocks per the audit), but still rewrite tool blocks to text.
    Otherwise we collapse to a plain string.

    Graduated trimming: when ``recent_full`` > 0 the last ``recent_full``
    messages that carry a tool_result are kept at FULL size (no truncation),
    while OLDER tool-results are trimmed to ``max_tool_result_chars``. This
    protects the context the model is actively using while still cutting the
    cost of replaying ancient tool output on every turn.
    """
    # Indices of messages that contain a tool_result, so we can keep the most
    # recent few at full fidelity.
    protected: set = set()
    if recent_full > 0 and max_tool_result_chars > 0:
        tr_indices = []
        for mi, m in enumerate(messages):
            c = m.get("content")
            if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in c):
                tr_indices.append(mi)
        protected = set(tr_indices[-recent_full:])

    out: List[Dict[str, Any]] = []
    # Only an image in the CURRENT turn (the last message) should emit a
    # compression log line. Images replayed from history are compressed
    # silently so the live log is not spammed every turn.
    _last_idx = len(messages) - 1
    for idx, m in enumerate(messages):
        role = m.get("role", "user")
        content = m.get("content")
        _log_img = (idx == _last_idx)
        # Recent tool-results keep full fidelity; older ones get trimmed.
        eff_chars = 0 if idx in protected else max_tool_result_chars

        if not flatten_tools:
            out.append({"role": role, "content": content})
            continue

        if message_has_image(content) and isinstance(content, list):
            new_blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    new_blocks.append(_compress_image_block(b, log=_log_img))
                elif (isinstance(b, dict) and b.get("type") == "tool_result"
                      and isinstance(b.get("content"), list)
                      and any(isinstance(ib, dict) and ib.get("type") == "image"
                              for ib in b["content"])):
                    # A tool_result carrying an image (e.g. a screenshot tool).
                    # Emit a short text marker for the tool_result envelope,
                    # then the image(s) as real, COMPRESSED image blocks so the
                    # model can actually see them without the base64 bloating
                    # the context. Non-image parts stay as text.
                    tcid = b.get("tool_use_id", "")
                    tag = "TOOL ERROR" if b.get("is_error") else "TOOL RESULT"
                    txt_parts: List[str] = []
                    imgs: List[Dict[str, Any]] = []
                    for ib in b["content"]:
                        if isinstance(ib, dict) and ib.get("type") == "image":
                            imgs.append(_compress_image_block(ib, log=_log_img))
                        elif isinstance(ib, dict) and ib.get("type") == "text":
                            txt_parts.append(ib.get("text", ""))
                        else:
                            txt_parts.append(str(ib))
                    marker = f"[{tag} for {tcid}]"
                    if txt_parts:
                        marker += "\n" + "\n".join(p for p in txt_parts if p)
                    new_blocks.append({"type": "text", "text": marker})
                    new_blocks.extend(imgs)
                else:
                    txt = flatten_content_blocks([b], role, eff_chars)
                    if txt:
                        new_blocks.append({"type": "text", "text": txt})
            out.append({"role": role, "content": new_blocks})
        else:
            out.append({"role": role,
                        "content": flatten_content_blocks(content, role,
                                                           eff_chars)})
    return out


def _msg_is_empty(content: Any) -> bool:
    """True if a message has no meaningful content (empty string / no blocks)."""
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "image":
                    return False
                if (b.get("text") or "").strip():
                    return False
            elif str(b).strip():
                return False
        return True
    return False


def _merge_two(a: Any, b: Any) -> Any:
    """Merge the content of two same-role messages into one."""
    if isinstance(a, list) or isinstance(b, list):
        la = a if isinstance(a, list) else [{"type": "text", "text": str(a)}]
        lb = b if isinstance(b, list) else [{"type": "text", "text": str(b)}]
        return la + lb
    return (str(a) + "\n\n" + str(b)).strip()


def normalize_alternating(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make a messages array satisfy the upstream's strict contract:
      1. no empty messages,
      2. the list STARTS with a 'user' message,
      3. roles strictly ALTERNATE user/assistant (consecutive same-role
         messages are merged into one).
    This is what prevents the recurring 400 'messages must have alternating
    user and assistant roles / message 0 is not a user message' error after
    history trimming.
    """
    # 1) drop empties
    cleaned = [m for m in messages if not _msg_is_empty(m.get("content"))]
    # 2) drop any leading non-user turns (orphaned assistant / tool_result)
    while cleaned and cleaned[0].get("role") != "user":
        cleaned.pop(0)
    # 3) merge consecutive same-role messages
    merged: List[Dict[str, Any]] = []
    for m in cleaned:
        role = m.get("role", "user")
        if merged and merged[-1].get("role") == role:
            merged[-1]["content"] = _merge_two(merged[-1].get("content"),
                                                m.get("content"))
        else:
            merged.append({"role": role, "content": m.get("content")})
    # 4) a trailing assistant message would leave the model nothing to answer;
    #    it's allowed by the API, so we keep it. But guarantee non-empty list.
    return merged


# --------------------------------------------------------------------------- #
# Parse the model's text output -> real content blocks (text + tool_use)
# --------------------------------------------------------------------------- #

def parse_tool_calls_from_text(text: str,
                               valid_names: Optional[set] = None,
                               strict: bool = True,
                               log_unknown: bool = True) -> List[Dict[str, Any]]:
    """Split raw model text into Anthropic content blocks, converting fenced
    tool_call JSON into tool_use blocks.

    If `strict` is True and `valid_names` is provided, a tool_call whose name
    is NOT in valid_names is left as plain text instead of being emitted as a
    tool_use block. This prevents the client from ever receiving an
    "unknown tool" call (the model occasionally invents or mistypes a name).
    """
    # --- NORMALIZE native XML tool calls into ```tool_call fences --------- #
    # The model frequently emits Anthropic's native <invoke>/<parameter> XML
    # instead of (or mixed with) our fence. Rewrite every complete <invoke>
    # block into an equivalent tool_call fence BEFORE the main parse, so a
    # single code path handles everything.
    if "invoke" in text and "<" in text:
        openers = list(INVOKE_NAME_RE.finditer(text))
        if openers:
            rebuilt: List[str] = []
            cursor = 0
            for i, om in enumerate(openers):
                rebuilt.append(text[cursor:om.start()])
                seg_end = openers[i + 1].start() if i + 1 < len(openers) else len(text)
                segment = text[om.start():seg_end]
                obj = _parse_invoke_xml(segment)
                if obj:
                    try:
                        js = json.dumps(obj, ensure_ascii=False)
                        rebuilt.append("\n```tool_call\n" + js + "\n```\n")
                    except Exception:
                        rebuilt.append(segment)
                else:
                    rebuilt.append(segment)
                cursor = seg_end
            rebuilt.append(text[cursor:])
            text = "".join(rebuilt)
    # Strip any ORPHAN closing tags left behind when the model mixed a JSON
    # fence with trailing </parameter></invoke> noise (the exact corruption
    # that made calls leak as code blocks).
    if "</" in text:
        text = re.sub(r"</(?:antml:)?(?:parameter|invoke)>", "", text)
        text = re.sub(r"<(?:antml:)?(?:parameter|invoke)\b[^>]*>", "", text)

    blocks: List[Dict[str, Any]] = []
    last = 0
    _match_count = 0
    for m in TOOL_CALL_FENCE_RE.finditer(text):
        _match_count += 1
        pre = text[last:m.start()].strip()
        raw = m.group(1).strip()
        parsed = _loads_tool_json(raw)
        name = parsed.get("name") if isinstance(parsed, dict) else None
        # Only treat as a tool call if it has a name key AND (when strict) the
        # name is one the client actually offered.
        name_ok = bool(name)
        if strict and valid_names is not None:
            name_ok = name in valid_names
        if isinstance(parsed, dict) and name_ok:
            if pre:
                blocks.append({"type": "text", "text": pre})
            tool_input = parsed.get("input", parsed.get("arguments", {}))
            if not isinstance(tool_input, dict):
                tool_input = {}
            blocks.append({
                "type": "tool_use",
                "id": "toolu_" + uuid.uuid4().hex[:24],
                "name": name,
                "input": tool_input,
            })
            last = m.end()
        elif isinstance(parsed, dict) and name and not name_ok:
            # Unknown tool name -> keep the whole fenced block as literal text
            # so the client never sees an invalid tool_use. Log it for the admin
            # -- but ONLY when this parse is the authoritative client-tools pass.
            # The server-web-tool probe (_find_server_tool_call) re-parses the
            # SAME text against a tiny set (just fetch_image); logging there would
            # flag every legitimate client tool (pwsh/read/grep...) as "unknown"
            # even though it executes fine on the main pass -> pure log noise.
            if log_unknown:
                log_event({"unknown_tool": name,
                           "note": "model called an unlisted tool -> kept as text"})
            # leave `last` as-is so the fence text falls into the tail/pre of
            # the next iteration; advance only past the preamble text we consumed
        # else: not a tool call -> leave as text (handled by tail)
        else:
            # DIAGNOSTIC: a fenced block matched the tool_call regex but we could
            # NOT turn it into a tool_use (parse failed, or no name). This is
            # exactly the "shows as code block" case. Capture the raw so we can
            # see what the model actually emitted.
            log_event({"parse_miss": True,
                       "parsed_ok": isinstance(parsed, dict),
                       "name_seen": name,
                       "raw_head": raw[:600],
                       "raw_tail": raw[-200:] if len(raw) > 200 else "",
                       "note": "fence matched but not emitted as tool_use"})
    # FALLBACK: the model emitted a BARE JSON tool call with no fence at all.
    # Only engage when NO fenced tool_use was produced, and only accept an
    # object whose "name" is a real offered tool -- so ordinary prose or JSON
    # data is never mistaken for a call.
    produced_tool = any(b.get("type") == "tool_use" for b in blocks)
    if not produced_tool and '"name"' in text:
        for cstart, cend, cand in _iter_balanced_json_objects(text):
            parsed = _loads_tool_json(cand.strip())
            name = parsed.get("name") if isinstance(parsed, dict) else None
            if not name:
                continue
            if strict and valid_names is not None and name not in valid_names:
                continue
            tool_input = parsed.get("input", parsed.get("arguments", {})) if isinstance(parsed, dict) else {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            pre = text[last:cstart].strip()
            if pre:
                blocks.append({"type": "text", "text": pre})
            blocks.append({
                "type": "tool_use",
                "id": "toolu_" + uuid.uuid4().hex[:24],
                "name": name,
                "input": tool_input,
            })
            last = cend
            produced_tool = True
            break

    tail = text[last:].strip()
    if tail:
        blocks.append({"type": "text", "text": tail})
    if not blocks:
        blocks.append({"type": "text", "text": text})
    # DIAGNOSTIC: the regex found NO fence at all, yet the text clearly contains
    # a tool-call-ish fenced block. That means the FENCE REGEX failed to match
    # (not the JSON parser). Capture the raw so we can fix the pattern.
    if _match_count == 0 and ("```tool_call" in text or '"name"' in text):
        snip = text
        idx = text.find("```")
        if idx > 0:
            snip = text[max(0, idx - 40):]
        log_event({"regex_miss": True,
                   "text_len": len(text),
                   "snip_head": snip[:700],
                   "note": "fence present but TOOL_CALL_FENCE_RE matched nothing"})
    return blocks


# --------------------------------------------------------------------------- #
# Post-processing of the upstream non-stream response
# --------------------------------------------------------------------------- #

def approx_tokens(text: str) -> int:
    # rough heuristic ~4 chars/token
    return max(1, int(len(text) / 4))


def apply_stop_sequences(text: str,
                         stop_sequences: Optional[List[str]]) -> Tuple[str, Optional[str]]:
    if not stop_sequences:
        return text, None
    cut_idx = None
    matched = None
    for s in stop_sequences:
        if not s:
            continue
        i = text.find(s)
        if i != -1 and (cut_idx is None or i < cut_idx):
            cut_idx = i
            matched = s
    if cut_idx is not None:
        return text[:cut_idx], matched
    return text, None


def normalize_usage(raw_usage: Dict[str, Any],
                    feats: Dict[str, Any],
                    output_text_len_tokens: int) -> Dict[str, Any]:
    mode = feats.get("usage_mode", "normalized")
    baseline = int(feats.get("usage_baseline_tokens", 0) or 0)
    raw_in = int(raw_usage.get("input_tokens", 0) or 0)
    raw_out = int(raw_usage.get("output_tokens", 0) or 0)

    if mode == "raw":
        return {"input_tokens": raw_in, "output_tokens": raw_out}
    if mode == "estimated":
        return {"input_tokens": max(0, raw_in - baseline),
                "output_tokens": raw_out or output_text_len_tokens}
    # normalized: subtract baseline from input, keep real output
    norm_in = raw_in
    # upstream sometimes reports input_tokens as the big combined number
    if raw_in >= baseline:
        norm_in = raw_in - baseline
    return {"input_tokens": max(0, norm_in),
            "output_tokens": raw_out or output_text_len_tokens}


def process_upstream_message(upstream: Dict[str, Any],
                             client_req: Dict[str, Any],
                             feats: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the upstream non-stream message into a clean Anthropic message,
    applying all fixes. Returns a dict ready to serialize / to stream."""
    content_in = upstream.get("content", []) or []

    # 1) Collect raw text, drop/stash thinking, keep upstream-native tool_use.
    text_segments: List[str] = []
    native_tool_blocks: List[Dict[str, Any]] = []
    had_thinking = False
    for b in content_in:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            text_segments.append(b.get("text", ""))
        elif bt == "thinking":
            had_thinking = True
            if not feats.get("strip_thinking", True):
                # keep thinking as its own block (verbatim) at the front
                native_tool_blocks.append(b)
        elif bt == "tool_use":
            # upstream emitted its OWN tool (e.g. system_todo_write) — keep it,
            # client asked for its own tools so this is rare; pass through.
            native_tool_blocks.append(b)

    joined_text = "\n\n".join(t for t in text_segments if t != "")

    # 2) stop_sequences
    stop_seq_matched = None
    if feats.get("enforce_stop_sequences", True):
        joined_text, stop_seq_matched = apply_stop_sequences(
            joined_text, client_req.get("stop_sequences"))

    # 3) parse tool_call fences from text -> blocks
    client_tools = client_req.get("tools") or []
    client_has_tools = bool(client_tools) and feats.get("tool_injection", True)
    # Closed set of names the client actually offered.
    valid_names = {t.get("name") for t in client_tools if t.get("name")}
    # Only enforce the closed set when the client gave us a list to check against.
    strict = feats.get("strict_tool_names", True) and bool(valid_names)
    if client_has_tools:
        parsed_blocks = parse_tool_calls_from_text(
            joined_text, valid_names=valid_names, strict=strict)
    else:
        parsed_blocks = [{"type": "text", "text": joined_text}] if joined_text else []

    # If strict, drop any upstream-native tool_use whose name isn't offered by
    # the client (prevents the client receiving an "unknown tool" block).
    if strict:
        kept = []
        for b in native_tool_blocks:
            if b.get("type") == "tool_use" and b.get("name") not in valid_names:
                log_event({"unknown_tool": b.get("name"),
                           "note": "upstream-native tool not offered by client -> dropped"})
                continue
            kept.append(b)
        native_tool_blocks = kept

    # merge: thinking (if kept) + native tool blocks come first, then parsed
    content_out: List[Dict[str, Any]] = []
    for b in native_tool_blocks:
        content_out.append(b)
    content_out.extend(parsed_blocks)
    if not content_out:
        content_out = [{"type": "text", "text": ""}]

    # 3b) ONE-TOOL-PER-TURN (agentic step mode).
    # A single response that fires several tools at once takes a long time to
    # generate upstream and is the main cause of the relay's ~100s 524 timeout
    # on long chains. Keep everything up to and INCLUDING the first tool_use,
    # then drop any further blocks. The client runs that one tool, returns its
    # result, and the model continues from there -- small, fast, visible steps.
    if feats.get("one_tool_per_turn", True):
        trimmed: List[Dict[str, Any]] = []
        dropped = 0
        seen_tool = False
        for b in content_out:
            if seen_tool:
                # already emitted the first tool -> drop everything after it
                if b.get("type") == "tool_use":
                    dropped += 1
                continue
            trimmed.append(b)
            if b.get("type") == "tool_use":
                seen_tool = True
        if dropped > 0:
            log_event({"one_tool_per_turn": True, "dropped_tool_calls": dropped,
                       "note": "kept first tool_use, dropped extras -> single step"})
            content_out = trimmed

    # 4) determine stop_reason
    has_tool_use = any(b.get("type") == "tool_use" for b in content_out)
    stop_reason = "end_turn"
    stop_sequence_val = None
    if has_tool_use:
        stop_reason = "tool_use"
    if stop_seq_matched is not None:
        stop_reason = "stop_sequence"
        stop_sequence_val = stop_seq_matched

    # 5) max_tokens enforcement (only meaningful for text)
    max_tokens = client_req.get("max_tokens")
    if (feats.get("enforce_max_tokens", True) and isinstance(max_tokens, int)
            and max_tokens > 0 and stop_reason == "end_turn" and not has_tool_use):
        # truncate the LAST text block by approx token budget
        total = 0
        truncated = False
        new_content = []
        for b in content_out:
            if b.get("type") == "text":
                words = b["text"]
                tk = approx_tokens(words)
                if total + tk > max_tokens:
                    budget_chars = max(0, (max_tokens - total) * 4)
                    b = {"type": "text", "text": words[:budget_chars]}
                    truncated = True
                    total = max_tokens
                    new_content.append(b)
                    break
                total += tk
            new_content.append(b)
        if truncated:
            content_out = new_content
            stop_reason = "max_tokens"

    # 6) usage
    out_tok_est = approx_tokens(joined_text)
    usage = normalize_usage(upstream.get("usage", {}) or {}, feats, out_tok_est)

    result = {
        "id": upstream.get("id", "msg_" + uuid.uuid4().hex[:16]),
        "type": "message",
        "role": "assistant",
        "model": client_req.get("model", upstream.get("model", CONFIG["model"])),
        "content": content_out,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence_val,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }
    # Names (+ categories) of the tools the model actually CALLED on this turn,
    # so the dashboard row can show WHICH tool ran (e.g. 'read', 'pwsh') instead
    # of just a generic 'tool' tag.
    _called = [b.get("name") for b in content_out
               if b.get("type") == "tool_use" and b.get("name")]
    result["_meta"] = {
        "had_thinking": had_thinking,
        "has_tool_use": has_tool_use,
        "client_tools": len(client_req.get("tools") or []),
        "tool_names": _called,
        "tool_categories": sorted({classify_tool(n) for n in _called}),
    }
    return result


# --------------------------------------------------------------------------- #
# Server-side web tools: executed BY the proxy (no client involvement).
# The proxy injects these tool definitions, the model emits a tool_call,
# we run it here (DuckDuckGo HTML endpoint -> no API key), then feed the
# result back as a user turn and loop until the model answers normally.
# --------------------------------------------------------------------------- #

# Tool schemas injected into the system prompt (same shape as a client tool).
SERVER_WEB_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fetch_image",
        "description": (
            "Fetch an image by URL so you can SEE and analyse it. Returns the "
            "image to you directly. Use to describe, read, or reason about an "
            "image available online."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL of the image."},
            },
            "required": ["url"],
        },
    },
]

SERVER_WEB_TOOL_NAMES = {t["name"] for t in SERVER_WEB_TOOLS}

# Headers specifically for image requests. Some CDNs (notably Wikimedia) block
# any request whose User-Agent is not a descriptive bot UA carrying contact
# info (their published robot policy returns 403 "respect our robot policy"
# otherwise). We therefore advertise a descriptive UA with a contact URL plus
# an image-first Accept header. This satisfies Wikimedia and ordinary CDNs.
_IMAGE_HEADERS = {
    "User-Agent": ("jdw-fix-proxy/1.0 "
                   "(+https://github.com/jdw-fix-proxy; contact: admin@localhost) "
                   "httpx"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

# [web search helpers removed: only image fetching remains]


async def _fetch_image_block(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    """Fetch an image and return an Anthropic image content block (base64)."""
    # Build image-appropriate headers, adding a same-origin Referer which some
    # CDNs (Wikimedia, etc.) require before they serve the bytes.
    hdrs = dict(_IMAGE_HEADERS)
    try:
        pr = urlparse(url)
        if pr.scheme and pr.netloc:
            hdrs["Referer"] = f"{pr.scheme}://{pr.netloc}/"
    except Exception:
        pass
    # Independent, SHORT timeout for image fetches. We must never inherit the
    # big upstream model timeout here: a hanging image host would otherwise
    # freeze the whole turn for upstream_timeout_s, repeated up to
    # web_tools_max_iters times. A dedicated per-call client guarantees the
    # ceiling regardless of what `client` was configured with.
    img_timeout = httpx.Timeout(IMG_FETCH_TOTAL_S,
                                connect=IMG_FETCH_CONNECT_S,
                                read=IMG_FETCH_READ_S)
    # Early size reject via a streamed read so we abort BEFORE buffering a
    # multi-megabyte body into memory / base64.
    try:
        async with httpx.AsyncClient(timeout=img_timeout,
                                     follow_redirects=True) as _ic:
            async with _ic.stream("GET", url, headers=hdrs) as r:
                if r.status_code >= 400:
                    return {"_error": f"fetch_image HTTP {r.status_code} for {url}"}
                # Trust Content-Length when present to reject huge files up front.
                _clen = r.headers.get("content-length")
                if _clen and _clen.isdigit() and int(_clen) > IMG_FETCH_MAX_BYTES:
                    return {"_error":
                            f"fetch_image: image too large ({int(_clen)} bytes > "
                            f"{IMG_FETCH_MAX_BYTES} limit)"}
                ctype = (r.headers.get("content-type", "") or "").split(";")[0].strip().lower()
                chunks: List[bytes] = []
                total = 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > IMG_FETCH_MAX_BYTES:
                        return {"_error":
                                f"fetch_image: image exceeded {IMG_FETCH_MAX_BYTES} "
                                f"byte limit while downloading"}
                    chunks.append(chunk)
                content = b"".join(chunks)
    except httpx.TimeoutException:
        return {"_error": f"fetch_image timed out after {IMG_FETCH_TOTAL_S}s for {url}"}
    except Exception as e:
        return {"_error": f"fetch_image error: {e}"}
    if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        # best-effort guess from extension
        low = url.lower()
        if low.endswith((".jpg", ".jpeg")):
            ctype = "image/jpeg"
        elif low.endswith(".png"):
            ctype = "image/png"
        elif low.endswith(".gif"):
            ctype = "image/gif"
        elif low.endswith(".webp"):
            ctype = "image/webp"
        else:
            return {"_error": f"fetch_image: unsupported content-type '{ctype}'"}
    b64 = base64.b64encode(content).decode("ascii")
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": ctype, "data": b64},
    }
    # Downscale/recompress exactly like inbound images so a big fetched image
    # can't blow up the context (reuses the shared Pillow helper; no-ops when
    # Pillow is missing or compression wouldn't help).
    return _compress_image_block(block)


async def run_server_web_tool(client: httpx.AsyncClient,
                              name: str,
                              tool_input: Dict[str, Any],
                              feats: Dict[str, Any]) -> Dict[str, Any]:
    """Execute ONE server-side web tool. Returns a dict:
        {"text": str}                      -> feed back as text
        {"blocks": [image_block, ...]}     -> feed back as content blocks
    """
    if name == "fetch_image":
        url = (tool_input.get("url") or "").strip()
        if not url:
            return {"text": "[fetch_image: empty url]"}
        block = await _fetch_image_block(client, url)
        if block.get("_error"):
            return {"text": "[" + block["_error"] + "]"}
        return {"blocks": [
            {"type": "text", "text": f"Fetched image from {url}:"},
            block,
        ]}
    return {"text": f"[unknown server tool: {name}]"}


def _raw_assistant_text(upstream: Dict[str, Any]) -> str:
    """Join the plain-text blocks of an upstream message (ignoring thinking)."""
    segs: List[str] = []
    for b in (upstream.get("content") or []):
        if isinstance(b, dict) and b.get("type") == "text":
            segs.append(b.get("text", ""))
    return "\n\n".join(s for s in segs if s)


def _find_server_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """Return the FIRST server-tool tool_use parsed from the model's text, or
    None. Server tools are always valid names here (strict against our set)."""
    if not text:
        return None
    blocks = parse_tool_calls_from_text(
        text, valid_names=SERVER_WEB_TOOL_NAMES, strict=True, log_unknown=False)
    for b in blocks:
        if b.get("type") == "tool_use" and b.get("name") in SERVER_WEB_TOOL_NAMES:
            return b
    return None


async def resolve_server_tools(payload: Dict[str, Any],
                              feats: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Call upstream; if the model invokes one of OUR server web tools, execute
    it here, feed the result back, and loop -- until the model answers without
    a server-tool call (or we hit the iteration cap). The returned upstream
    message is then processed normally for the client.

    When web tools are disabled this is a thin pass-through to call_upstream.
    """
    status, upstream = await call_upstream(payload)
    if not feats.get("web_tools_enabled", False):
        return status, upstream
    if status >= 400 or not isinstance(upstream, dict):
        return status, upstream

    max_iters = int(feats.get("web_tools_max_iters", 4) or 4)
    timeout = httpx.Timeout(float(CONFIG.get("upstream_timeout_s", 300)))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as web_client:
        for _it in range(max_iters):
            raw_text = _raw_assistant_text(upstream)
            call = _find_server_tool_call(raw_text)
            if not call:
                break  # model answered (or called a CLIENT tool) -> hand off
            name = call.get("name")
            tin = call.get("input") or {}
            log_event({"server_tool": name, "input": tin, "iter": _it,
                       "note": "proxy executing server-side web tool"})
            try:
                res = await run_server_web_tool(web_client, name, tin, feats)
            except Exception as e:
                res = {"text": f"[{name} failed: {e}]"}
            # Append the model's own turn, then the tool result as a user turn,
            # preserving start-with-user + strict alternation.
            payload["messages"].append({"role": "assistant", "content": raw_text})
            if res.get("blocks"):
                user_content: Any = res["blocks"]
            else:
                user_content = (res.get("text") or "")
            payload["messages"].append({"role": "user", "content": user_content})
            status, upstream = await call_upstream(payload)
            if status >= 400 or not isinstance(upstream, dict):
                return status, upstream
        else:
            # Loop ran to the cap without the model finishing: nudge it to answer
            # using what it already gathered (no more tool calls).
            payload["messages"].append({
                "role": "user",
                "content": ("You have reached the web-tool limit. Answer now "
                            "using the information already gathered; do not call "
                            "any more tools."),
            })
            status, upstream = await call_upstream(payload)
    return status, upstream


# --------------------------------------------------------------------------- #
# Build the upstream payload
# --------------------------------------------------------------------------- #

def build_upstream_payload(client_req: Dict[str, Any],
                           feats: Dict[str, Any]) -> Dict[str, Any]:
    model = client_req.get("model") or CONFIG["model"]

    # system prompt (+ tool injection)
    system_text = extract_system_text(client_req.get("system"))
    tools = client_req.get("tools") or []
    # Hard-drop web_search: never advertise it to the model (it needs an API
    # key the deployment doesn't have, so it only wastes tokens / confuses).
    tools = [t for t in tools if (t.get("name") or "").lower() != "web_search"]
    tool_choice = client_req.get("tool_choice")
    addendum = ""
    # Server-side web tools are injected IN ADDITION to the client's tools when
    # enabled -- but NOT when the client forces one specific tool this turn.
    forcing_tool = (isinstance(tool_choice, dict) and tool_choice.get("type") == "tool"
                    and bool(tool_choice.get("name")))
    web_on = bool(feats.get("web_tools_enabled", False)) and not forcing_tool
    if feats.get("tool_injection", True) and (tools or web_on):
        # If the client forces ONE specific tool, only inject that tool's schema
        # (big saving vs. injecting all N tool schemas every request).
        inject_tools = list(tools)
        if forcing_tool:
            only = [t for t in tools if t.get("name") == tool_choice["name"]]
            if only:
                inject_tools = only
        elif web_on:
            # Append our server tools, skipping any the client already defines
            # under the same name (client definition wins).
            client_names = {t.get("name") for t in tools}
            for st in SERVER_WEB_TOOLS:
                if st["name"] not in client_names:
                    inject_tools.append(st)
        addendum = build_tool_system_block(
            inject_tools, tool_choice,
            compact=feats.get("compact_tool_schemas", True),
            ultra=feats.get("ultra_compact_tools", False),
            one_tool=feats.get("one_tool_per_turn", True))
        system_text = (system_text + "\n\n" + addendum) if system_text else addendum

    # messages (flatten tool blocks for upstream comprehension)
    all_msgs = client_req.get("messages", []) or []
    # Optionally keep only the last N messages to cap history token cost.
    # IMPORTANT: the upstream requires the message list to START with a
    # 'user' message and alternate roles. A naive messages[-N:] slice can
    # start on an 'assistant' (or an orphaned tool_result) and get rejected
    # with a 400. So we keep the FIRST user message (original task) and the
    # last N, and we make sure the trimmed tail begins on a user turn.
    max_hist = int(feats.get("max_history_messages", 0) or 0)
    if max_hist > 0 and len(all_msgs) > max_hist:
        tail = all_msgs[-max_hist:]
        # Drop leading turns until the tail starts with a 'user' message
        # (also drops orphaned tool_result blocks that lost their tool_use).
        while tail and tail[0].get("role") != "user":
            tail = tail[1:]
        # Preserve the very first user message (the original task/context)
        # if it is not already included in the tail.
        # NOTE: use IDENTITY (`is`), not `in tail`. `x in tail` does a deep
        # equality compare of the first message against EVERY tail message;
        # with large contexts (big tool results / base64 images) that is an
        # O(N * payload) scan that itself adds latency to exactly the heavy
        # requests we are trying to keep fast. Identity is O(N) pointer checks
        # and is correct here because `tail` is a slice of the SAME objects.
        head = []
        if all_msgs and all_msgs[0].get("role") == "user" \
                and not any(all_msgs[0] is _t for _t in tail):
            head = [all_msgs[0]]
        all_msgs = head + tail
    # Final guard: whatever we send must begin with a user message.
    while all_msgs and all_msgs[0].get("role") != "user":
        all_msgs = all_msgs[1:]
    # DIAGNOSTIC: measure which tools dominate the outbound payload so a slow /
    # stalling request can be traced to the exact tool. Logs the top offenders
    # plus a per-tool aggregate. Cheap (string length only); safe to keep on.
    try:
        _sizes = _tool_payload_sizes(all_msgs)
        if _sizes:
            # Per-tool aggregates: total bytes + how many times each tool ran.
            # A tool_use is one CALL; its matching tool_result is not counted
            # again as a call (we count 'input' kinds only for the call tally).
            _agg: Dict[str, int] = {}
            _calls: Dict[str, int] = {}
            _cat_bytes: Dict[str, int] = {}
            _cat_calls: Dict[str, int] = {}
            for _r in _sizes:
                _nm, _cat = _r["tool"], _r["category"]
                _agg[_nm] = _agg.get(_nm, 0) + _r["bytes"]
                _cat_bytes[_cat] = _cat_bytes.get(_cat, 0) + _r["bytes"]
                if _r["kind"] == "input":
                    _calls[_nm] = _calls.get(_nm, 0) + 1
                    _cat_calls[_cat] = _cat_calls.get(_cat, 0) + 1
            _agg_sorted = sorted(_agg.items(), key=lambda kv: kv[1], reverse=True)
            _cat_sorted = sorted(_cat_bytes.items(), key=lambda kv: kv[1], reverse=True)
            log_event({
                "tool_payload_scan": True,
                "total_tool_bytes": sum(_r["bytes"] for _r in _sizes),
                "tool_count": len(_sizes),
                # distinct tool names that actually ran, with how many times each
                "tools_used": sorted(_calls.keys()),
                "calls_by_tool": dict(sorted(_calls.items(),
                                             key=lambda kv: kv[1], reverse=True)),
                # the biggest individual contributions (which call bloated it)
                "top_items": [
                    {"tool": _r["tool"], "category": _r["category"],
                     "kind": _r["kind"], "bytes": _r["bytes"]}
                    for _r in _sizes[:5]
                ],
                "by_tool_bytes": {k: v for k, v in _agg_sorted[:8]},
                # the classification the user asked for: bytes + calls per group
                "by_category_bytes": {k: v for k, v in _cat_sorted},
                "by_category_calls": dict(sorted(_cat_calls.items(),
                                                 key=lambda kv: kv[1], reverse=True)),
            })
    except Exception as _e:
        log_event({"tool_payload_scan_error": str(_e)[:200]})
    # De-duplicate repeated tool-results (older copies -> short marker) BEFORE
    # flattening, so a file re-read many times is only paid for once.
    if feats.get("dedup_tool_results", True):
        try:
            all_msgs = dedup_tool_results(all_msgs)
        except Exception as _e:
            log_event({"dedup_tool_results_error": str(_e)[:200]})
    msgs = flatten_messages(all_msgs,
                            feats.get("flatten_tool_history", True),
                            int(feats.get("max_tool_result_chars", 0) or 0),
                            int(feats.get("recent_tool_results_full", 0) or 0))
    # Enforce the upstream's strict contract (start-with-user + strict
    # alternation). This is the definitive fix for the recurring 400
    # 'messages must have alternating user and assistant roles' error that
    # history trimming / tool-result flattening can otherwise trigger.
    msgs = normalize_alternating(msgs)

    # max_tokens: never send a tiny budget upstream, otherwise the model can
    # get cut off MID tool_call (the fenced JSON is left unterminated and can't
    # be parsed into a tool_use -> the tool silently "stops half way"). We
    # raise the upstream budget to a safe floor so tool calls always complete;
    # our own enforce_max_tokens step still trims pure-text replies afterwards.
    _client_max = client_req.get("max_tokens", 4096) or 4096
    _upstream_max = _client_max if _client_max >= 4096 else 4096
    # Admin hard cap on OUTPUT tokens. When set (>0) it OVERRIDES the client's
    # request and clamps generation to this value, capping cost per turn.
    _hard_cap = int(feats.get("max_output_tokens", 0) or 0)
    if _hard_cap > 0:
        _upstream_max = _hard_cap
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": _upstream_max,
        "messages": msgs,
        "stream": False,  # ALWAYS non-stream upstream
    }
    if system_text:
        payload["system"] = system_text
    # pass temperature/top_p/top_k through (they work per audit)
    for k in ("temperature", "top_p", "top_k"):
        if k in client_req and client_req[k] is not None:
            payload[k] = client_req[k]
    # NOTE: we intentionally do NOT forward client tools/tool_choice/stop_sequences
    # to the upstream (it discards them); we handle them ourselves.

    # Approx token breakdown so the dashboard can show WHERE the input cost is
    # (tools vs. history vs. base system) -> helps diagnose high usage.
    tools_tok = approx_tokens(addendum) if (feats.get("tool_injection", True) and tools) else 0
    base_sys_tok = approx_tokens(system_text) - tools_tok
    hist_tok = approx_tokens(json.dumps(msgs, ensure_ascii=False))
    payload["_breakdown"] = {
        "tools_tok": max(0, tools_tok),
        "system_tok": max(0, base_sys_tok),
        "history_tok": max(0, hist_tok),
        "history_msgs": len(msgs),
    }
    return payload


# --------------------------------------------------------------------------- #
# SSE synthesis
# --------------------------------------------------------------------------- #

def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def synthesize_sse(message: Dict[str, Any], emit_start: bool = True):
    """Yield a spec-correct Anthropic SSE stream from a finished message dict.

    When `emit_start` is False the leading `message_start` + `ping` events are
    skipped (used by stream_with_keepalive, which already opened the stream).
    """
    usage = message.get("usage", {})
    msg_id = message.get("id")
    model = message.get("model")

    if emit_start:
        # message_start (with real input usage)
        start_msg = {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": 0,
                },
            },
        }
        yield sse("message_start", start_msg)
        yield sse("ping", {"type": "ping"})

    content = message.get("content", [])
    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            text = block.get("text", "")
            # chunk text so clients render progressively
            for chunk in chunk_text(text, 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": chunk},
                })
                await asyncio.sleep(0)
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

        elif btype == "tool_use":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            })
            partial = json.dumps(block.get("input", {}), ensure_ascii=False)
            for chunk in chunk_text(partial, 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": chunk},
                })
                await asyncio.sleep(0)
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

        elif btype == "thinking":
            yield sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            for chunk in chunk_text(block.get("thinking", ""), 60):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": chunk},
                })
                await asyncio.sleep(0)
            if block.get("signature"):
                yield sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "signature_delta",
                              "signature": block["signature"]},
                })
            yield sse("content_block_stop",
                      {"type": "content_block_stop", "index": idx})

    # message_delta with final stop_reason + output usage
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason"),
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield sse("message_stop", {"type": "message_stop"})


def _salvage_truncated(upstream: Any) -> str:
    """Recover assistant text from a relay body that was truncated mid-JSON.

    Heavy requests (large context + thinking + stream) sometimes get their
    response cut off by the relay, leaving an unparseable body like
    `{"content": [{"text": "real answer ...` with no closing braces. Rather
    than fail the whole turn, pull the partial text out so the user still gets
    the answer that was already generated.

    Returns the recovered text, or "" if nothing usable could be extracted.
    """
    if not isinstance(upstream, dict):
        return ""
    raw = upstream.get("_raw")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    # Only salvage bodies that actually look like a (partial) assistant reply.
    if '"text"' not in raw:
        return ""
    parts: List[str] = []
    # Walk every `"text": "..."` chunk, decoding JSON string escapes. The LAST
    # one may be unterminated (truncation point) -> take whatever is there.
    i = 0
    marker = '"text":'
    while True:
        j = raw.find(marker, i)
        if j < 0:
            break
        k = raw.find('"', j + len(marker))
        if k < 0:
            break
        # scan the string body, honouring backslash escapes, until an
        # unescaped closing quote OR end-of-buffer (truncated).
        buf = []
        p = k + 1
        n = len(raw)
        closed = False
        while p < n:
            c = raw[p]
            if c == "\\" and p + 1 < n:
                buf.append(raw[p:p + 2]); p += 2; continue
            if c == '"':
                closed = True; break
            buf.append(c); p += 1
        frag = "".join(buf)
        try:
            frag = json.loads('"' + frag + '"')
        except Exception:
            # last fragment truncated inside an escape -> best-effort cleanup
            try:
                frag = json.loads('"' + frag.rstrip("\\") + '"')
            except Exception:
                frag = frag.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
        if frag:
            parts.append(frag)
        i = p + 1 if closed else n
    text = "".join(parts).strip()
    return text


async def stream_with_keepalive(payload: Dict[str, Any],
                                client_req: Dict[str, Any],
                                feats: Dict[str, Any],
                                t0: float,
                                keepalive_s: float = 2.0,
                                breakdown: Optional[Dict[str, Any]] = None):
    """Streaming path that eliminates the "dead air" while the (non-stream)
    upstream is still composing its full reply.

    It opens the SSE stream to the client IMMEDIATELY (so the client sees bytes
    and never times out / feels stalled), emits a `ping` every `keepalive_s`
    seconds while call_upstream runs concurrently in the background, and only
    then streams the real content via synthesize_sse.
    """
    # Open the stream right away with a spec-correct message_start FIRST, so
    # the client sees bytes immediately and the ordering stays valid.
    provisional_id = "msg_" + uuid.uuid4().hex[:16]
    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": provisional_id,
            "type": "message",
            "role": "assistant",
            "model": payload.get("model") or CONFIG["model"],
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield sse("ping", {"type": "ping"})

    # Run the upstream call (incl. any server-side web-tool round-trips)
    # concurrently with the keepalive pings.
    task = asyncio.ensure_future(resolve_server_tools(payload, feats))
    try:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=keepalive_s)
            except asyncio.TimeoutError:
                # still waiting -> send a keepalive ping and loop
                yield sse("ping", {"type": "ping"})
            except Exception:
                # the task raised; break out and handle below via task.result()
                break

        status, upstream = task.result()
    except Exception as e:
        log_event({"model": payload.get("model"), "stream": True,
                   "error": f"upstream connection: {e}"})
        err = {"type": "error",
               "error": {"type": "api_error",
                         "message": f"Upstream connection failed: {e}"}}
        yield sse("error", err)
        return

    # CASE A: the relay returned a FULLY-FORMED assistant message but tagged it
    # with an error HTTP status (seen: a real {"content":[...]} body carrying a
    # 502/5xx code). The payload is perfectly usable -> treat it as success and
    # process it normally instead of throwing the good answer away.
    if status >= 400 and isinstance(upstream, dict) \
            and isinstance(upstream.get("content"), list) and upstream.get("content"):
        log_event({"model": payload.get("model"), "stream": True, "status": status,
                   "note": "usable content on error status -> recovered as success",
                   "recovered_blocks": len(upstream["content"])})
        status = 200

    if status >= 400:
        # LAST-RESORT SALVAGE: a heavy request (big context + thinking) can come
        # back with its JSON truncated mid-stream by the relay. If the truncated
        # body still carries real assistant text, recover that text and stream it
        # to the client instead of failing the whole turn.
        salvaged = _salvage_truncated(upstream)
        if salvaged:
            log_event({"model": payload.get("model"), "stream": True,
                       "status": 200, "note": "salvaged truncated upstream body",
                       "salvaged_chars": len(salvaged)})
            message = {"id": provisional_id, "type": "message", "role": "assistant",
                       "model": payload.get("model") or CONFIG["model"],
                       "content": [{"type": "text", "text": salvaged}],
                       "stop_reason": "end_turn", "stop_sequence": None,
                       "usage": {"input_tokens": 0, "output_tokens": 0}}
            async for evt in synthesize_sse(message, emit_start=False):
                yield evt
            return
        msg = ""
        if isinstance(upstream, dict):
            msg = (upstream.get("error", {}) or {}).get("message") \
                  or upstream.get("message") \
                  or upstream.get("_raw") \
                  or json.dumps(upstream, ensure_ascii=False)[:500]
        else:
            msg = str(upstream)[:500]
        log_event({"model": payload.get("model"), "stream": True,
                   "status": status, "error": (msg or "")[:200], "attempts": 5})
        etype = "invalid_request_error" if status == 400 else "api_error"
        yield sse("error", {"type": "error",
                            "error": {"type": etype,
                                      "message": f"Upstream {status} (after 5 attempt(s)): {msg}"}})
        return

    message = process_upstream_message(upstream, client_req, feats)
    meta = message.pop("_meta", {})
    dt = round(time.time() - t0, 2)
    log_event({
        "model": message.get("model"), "stream": True, "status": status,
        "latency_s": dt, "client_tools": meta.get("client_tools", 0),
        "tool_use": meta.get("has_tool_use", False),
        "tool_names": meta.get("tool_names", []),
        "tool_categories": meta.get("tool_categories", []),
        "had_thinking": meta.get("had_thinking", False),
        "stop_reason": message.get("stop_reason"),
        "usage": message.get("usage"),
        "breakdown": breakdown or {},
    })

    # Now stream the real content. message_start was already emitted above,
    # so skip it here to keep the SSE event ordering spec-correct.
    async for evt in synthesize_sse(message, emit_start=False):
        yield evt


def chunk_text(text: str, size: int):
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(title="JDW Fix Proxy")


def anthropic_error(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


async def _try_one_key(client: httpx.AsyncClient, url: str, api_key: str,
                       payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Optional[Exception]]:
    """Run the full retry loop for a SINGLE api key.

    Returns (status, data, last_exc). status == 0 means every attempt hit a
    network-level exception (last_exc carries the reason).
    """
    # Send BOTH auth styles: some upstreams expect Anthropic-native `x-api-key`,
    # others expect `Authorization: Bearer`. Sending both is safe and fixes
    # sporadic "API key is invalid" when the relay checks the other header.
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
    }
    # NOTE: 401/403 are NOT retried here -- a real key rejection is handled ONCE
    # by call_upstream (it moves to the fallback key). Retrying auth errors here
    # would multiply requests (3x per key x 2 keys = 6) and hammer an already
    # overloaded relay. We only retry genuine transient/capacity errors.
    retryable = {408, 409, 425, 429, 500, 502, 503, 504, 529}
    # Longer, growing backoff for capacity errors: hammering a relay that just
    # said "no available channel" only makes the shortage worse. Give it room
    # to recover between attempts instead of adding load.
    # Heavy requests (large context + thinking + stream) sometimes get their
    # response truncated mid-JSON by the relay. That is transient, so we retry
    # several times with growing back-off to give the relay time to free a slot.
    # ADAPTIVE BACKOFF. The relay rejects in two very different ways:
    #   * FAST reject: it returns an empty/403 body in well under a second,
    #     meaning "I have no free channel RIGHT NOW" -- it is NOT busy with us.
    #     The smart move is to retry almost immediately so we grab a channel
    #     the instant one frees up, instead of idling for seconds.
    #   * SLOW reject: it holds the request for many seconds and THEN returns a
    #     bad body -- that is genuine load, so we back off generously to give
    #     it room to recover.
    # This cuts the common fast-reject case from ~90s down to a few seconds
    # without adding any extra pressure during real congestion.
    slow_backoff = [1.0, 2.5, 4.0, 6.0]  # used when the previous reject was slow
    fast_delay = 0.5                      # used when the previous reject was fast
    FAST_THRESHOLD = 3.0                  # a reject under this many seconds = fast
    max_attempts = 7                      # more tries, but the fast ones are cheap
    # HARD WALL-CLOCK BUDGET. Without this, a persistently-dead relay channel
    # could burn ~3 minutes across 7 slow rejects + growing back-off before
    # giving up -- a terrible experience that just makes the caller wait for an
    # answer that was never coming. Once we've spent this long across all
    # attempts (including sleeps), stop retrying and surface a 502 immediately
    # so the client can react fast instead of hanging.
    TOTAL_BUDGET_S = 90.0
    loop_t0 = time.time()
    status = 0
    data: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    last_elapsed: Optional[float] = None  # how long the previous attempt took
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            # Pick the delay based on how the PREVIOUS attempt failed.
            if last_elapsed is not None and last_elapsed < FAST_THRESHOLD:
                delay = fast_delay
            else:
                delay = slow_backoff[min(attempt - 2, len(slow_backoff) - 1)]
            # If a capacity cooldown is active (the relay just ran out of
            # channels), wait at least until it expires -- retrying sooner only
            # piles more load onto a relay that is already starved. BUT a fast
            # reject means the relay answered instantly, so we don't inflate the
            # cooldown wait in that case; we only honour it for slow rejects.
            if not (last_elapsed is not None and last_elapsed < FAST_THRESHOLD):
                with _CAP_LOCK:
                    cd_remaining = _CAP_COOLDOWN_UNTIL - time.time()
                if cd_remaining > delay:
                    delay = min(cd_remaining, 9.0)
            # Enforce the hard wall-clock budget: if sleeping + another attempt
            # would push us past the budget, stop now and return what we have.
            # Giving up fast beats making the caller wait on a dead channel.
            elapsed_total = time.time() - loop_t0
            if elapsed_total + delay >= TOTAL_BUDGET_S:
                log_event({"retry_giveup": attempt, "after_status": status,
                           "elapsed_s": round(elapsed_total, 2),
                           "note": "total retry budget exhausted -> giving up"})
                break
            log_event({"retry": attempt, "after_status": status,
                       "sleep_s": round(delay, 2),
                       "prev_s": round(last_elapsed, 2) if last_elapsed is not None else None,
                       "note": "retry upstream"})
            await asyncio.sleep(delay)
        attempt_t0 = time.time()
        try:
            r = await client.post(url, headers=headers, json=payload)
        except Exception as e:  # network error -> retry
            last_exc = e
            status = 0
            last_elapsed = time.time() - attempt_t0  # usually slow (timeout)
            data = {"error": {"type": "api_error",
                              "message": f"upstream connection: {e}"}}
            continue
        last_elapsed = time.time() - attempt_t0
        last_exc = None
        status = r.status_code
        raw_text = r.text or ""
        bad_body = False
        if raw_text.strip() == "":
            # Upstream dropped the connection / returned an empty body.
            # This is a transient relay failure (seen as {"_raw": ""}),
            # NOT a real key error -> retry it.
            bad_body = True
            data = {"_raw": "",
                    "error": {"type": "api_error",
                              "message": "upstream returned an empty response body"}}
        else:
            try:
                data = r.json()
            except Exception as _pe:
                # Unparseable body: preserve RAW so the reason is visible,
                # and treat as transient -> retry.
                bad_body = True
                _ctype = r.headers.get("content-type", "")
                log_event({"parse_fail": True, "status": status,
                           "raw_len": len(raw_text),
                           "raw_head": raw_text[:120],
                           "raw_tail": raw_text[-200:],
                           "parse_err": str(_pe)[:200],
                           "ctype": _ctype})
                # The relay sits behind Cloudflare. On timeout/overload it returns
                # an HTML error page (e.g. 524 "A Timeout Occurred") instead of
                # JSON. Dumping 7KB of raw HTML at the client is useless noise, so
                # translate the common gateway statuses into a short, clear line.
                # NOTE: retry/timeout behaviour is unchanged -- bad_body stays
                # True, so long tasks still get the full retry budget.
                _is_html = ("text/html" in _ctype.lower()
                            or raw_text.lstrip()[:15].lower().startswith("<!doctype html")
                            or raw_text.lstrip()[:5].lower() == "<html")
                if _is_html:
                    _gw = {502: "bad gateway", 503: "service unavailable",
                           504: "gateway timeout",
                           520: "relay returned an unknown error",
                           521: "relay is down", 522: "connection timed out",
                           523: "relay is unreachable",
                           524: "relay timed out (model took too long or was overloaded)"}
                    _reason = _gw.get(status, "relay gateway error")
                    _clean = (f"Upstream relay returned an HTML error page "
                              f"(HTTP {status}: {_reason}). This is a transient "
                              f"relay/gateway problem, not a request error -- "
                              f"please retry.")
                else:
                    _clean = raw_text[:500]
                data = {"_raw": raw_text,
                        "error": {"type": "api_error",
                                  "message": _clean}}
            else:
                # Parsed OK but a 2xx with no usable content is also a
                # transient relay glitch -> retry.
                if status < 400 and isinstance(data, dict) \
                        and not data.get("content") and not data.get("error"):
                    bad_body = True
                # "No available channel" (and similar capacity messages) mean the
                # relay has no free upstream slot right now -> transient, retry it
                # instead of passing the error straight through to the client.
                if "no available channel" in raw_text.lower() \
                        or "no channel" in raw_text.lower():
                    bad_body = True

        # IMPORTANT: the real symptom of relay capacity exhaustion is NOT the
        # literal text "no available channel". In practice the relay returns
        # either a 200 with an EMPTY/contentless body, or a transient 403 with
        # an empty body. Both land here as bad_body. Treat ANY bad_body as a
        # capacity signal so the global throttle kicks in and OTHER concurrent
        # requests slow down, giving the relay room to recover.
        if bad_body:
            # Only treat a SLOW reject as real congestion that should throttle
            # OTHER concurrent requests. A fast reject just means "no free
            # channel this instant" -- inflating the global cooldown for that
            # would needlessly stall every other request.
            if last_elapsed is None or last_elapsed >= FAST_THRESHOLD:
                note_capacity_error()
        elif status < 400 and isinstance(data, dict) and data.get("content"):
            # A genuinely good, content-bearing response relieves the pressure.
            note_capacity_ok()

        if bad_body:
            log_event({"attempt": attempt, "status": status,
                       "note": "empty/invalid upstream body -> retry"})
            if attempt < max_attempts:
                continue
            # last attempt: an empty/invalid body is ALWAYS a transient relay
            # glitch (e.g. sporadic 403/empty or "no available channel"), never
            # a real key rejection -> surface as 502 so call_upstream does not
            # misclassify it as an auth failure / "invalid key".
            status = 502
            return status, data, last_exc

        if status < 400 or status not in retryable:
            return status, data, last_exc
        # retryable status -> loop again
    return status, data, last_exc


async def call_upstream(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Public entry: coalesce identical in-flight requests, then do the real
    upstream call exactly once per distinct payload. A second caller with the
    same payload awaits the first caller's result instead of paying for a
    duplicate upstream round-trip (saves tokens on double-submits/retries)."""
    fp = _payload_fingerprint(payload)
    async with _inflight_lock:
        fut = _inflight.get(fp)
        if fut is not None and not fut.done():
            # An identical request is already running -> reuse its result.
            log_event({"dedup_hit": True,
                       "note": "identical request already in flight -> "
                               "coalesced (no extra upstream tokens)"})
            waiter = fut
            owner = False
        else:
            waiter = asyncio.get_running_loop().create_future()
            _inflight[fp] = waiter
            owner = True
    if not owner:
        # Await the owner's result. If the owner failed, fall through to a
        # fresh independent call so this caller is never starved by that error.
        try:
            return await asyncio.shield(waiter)
        except Exception:
            return await _call_upstream_once(payload)
    # We own this fingerprint: run the real call and publish the result.
    try:
        result = await _call_upstream_once(payload)
        if not waiter.done():
            waiter.set_result(result)
        return result
    except Exception as e:
        if not waiter.done():
            waiter.set_exception(e)
        raise
    finally:
        async with _inflight_lock:
            if _inflight.get(fp) is waiter:
                _inflight.pop(fp, None)


async def _call_upstream_once(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    url = CONFIG["upstream_base_url"].rstrip("/") + "/messages"
    # Build the ordered, de-duplicated key list: primary first, then fallbacks.
    keys: List[str] = []
    primary = CONFIG.get("api_key", "") or ""
    if primary:
        keys.append(primary)
    for k in (CONFIG.get("fallback_api_keys") or []):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    if not keys:
        keys = [""]  # preserve old behaviour (send empty key, let upstream 401)

    # A genuine key rejection -> try the next key in the list.
    auth_reject = {401, 403}
    status = 0
    data: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    # Reuse the shared pooled client (keep-alive) instead of building a fresh
    # TCP+TLS connection per request, and gate on the concurrency semaphore so
    # we never ask the relay for more channels than it can serve at once.
    client = get_upstream_client()
    sem = get_upstream_sem()
    if sem is not None:
        async with sem:
            await _wait_capacity_slot()
            status, data = await _call_upstream_keys(client, url, keys,
                                                     payload, auth_reject)
    else:
        status, data = await _call_upstream_keys(client, url, keys, payload,
                                                 auth_reject)

    # Optional backup gateway: a cheap, independent safety net tried AT MOST
    # ONCE after the primary gives a CLEAR rejection or could not connect. We
    # deliberately do NOT replay timeouts / 5xx-but-maybe-delivered answers --
    # that risks paying twice for a response the primary may still return.
    if _backup_should_try(status):
        bstatus, bdata = await _call_backup_once(client, payload)
        if bstatus and bstatus < 400:
            return bstatus, bdata
        # Backup also failed -> surface the ORIGINAL primary result/error so
        # the client sees the real upstream reason, not the backup's.
        if bstatus:
            log_event({"note": "backup gateway also failed",
                       "backup_status": bstatus,
                       "primary_status": status})
    return status, data


def _backup_should_try(primary_status: int) -> bool:
    """Decide whether the backup gateway is eligible for THIS primary outcome.

    Only two situations justify a backup attempt:
      * status == 0   -> primary never delivered (connection/transport failure)
      * 400 <= status < 500 -> a CLEAR, non-transient rejection from the primary
    A 2xx is already a success, and 5xx/timeouts are treated as 'maybe the
    answer is still coming' so we never risk double-charging the user.
    """
    if not CONFIG.get("backup_enabled"):
        return False
    if not (CONFIG.get("backup_api_key") or "").strip():
        return False
    return primary_status == 0 or (400 <= primary_status < 500)


async def _call_backup_once(client: httpx.AsyncClient,
                            payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Fire the backup gateway exactly once (no key fan-out, no replay)."""
    base = (CONFIG.get("backup_base_url") or "").strip() \
        or CONFIG["upstream_base_url"]
    burl = base.rstrip("/") + "/messages"
    bkey = (CONFIG.get("backup_api_key") or "").strip()
    bmodel = (CONFIG.get("backup_model") or "").strip()
    # Only override the model when a backup model is explicitly configured;
    # otherwise keep whatever the client/primary payload already requested.
    bpayload = payload
    if bmodel:
        bpayload = dict(payload)
        bpayload["model"] = bmodel
    log_event({"note": "trying backup gateway (low-cost failover)",
               "backup_base": base, "backup_key_masked": masked_key(bkey),
               "backup_model": bmodel or "(same as request)"})
    status, data, last_exc = await _try_one_key(client, burl, bkey, bpayload)
    if status and status < 400:
        log_event({"key_used": "backup", "key_masked": masked_key(bkey),
                   "status": status, "note": "backup gateway OK"})
    elif status == 0 and last_exc is not None:
        log_event({"note": "backup gateway unreachable",
                   "error": str(last_exc)[:120]})
    return status, data


async def _call_upstream_keys(client: httpx.AsyncClient, url: str,
                              keys: List[str], payload: Dict[str, Any],
                              auth_reject: set) -> Tuple[int, Dict[str, Any]]:
    status = 0
    data: Dict[str, Any] = {}
    last_exc: Optional[Exception] = None
    for idx, key in enumerate(keys):
        if idx > 0:
            log_event({"fallback_key": idx, "after_status": status,
                       "note": "primary key rejected -> trying fallback key"})
        status, data, last_exc = await _try_one_key(client, url, key, payload)
        # Success or a real (non-auth) error -> stop, this is the answer.
        if status < 400 or status not in auth_reject:
            if last_exc is not None and status == 0:
                # all attempts for this key were network failures;
                # try the next key too (maybe a different endpoint path works)
                if idx < len(keys) - 1:
                    continue
                raise last_exc
            if status < 400:
                # Record which key actually worked (masked, never full).
                log_event({"key_used": "primary" if idx == 0 else f"fallback#{idx}",
                           "key_masked": masked_key(key), "status": status,
                           "note": "upstream OK with this key"})
            return status, data
        # auth rejection -> record the dead key, then loop to next one
        log_event({"key_rejected": "primary" if idx == 0 else f"fallback#{idx}",
                   "key_masked": masked_key(key), "status": status,
                   "note": "key rejected by upstream (401/403)"})
    if last_exc is not None and status == 0:
        raise last_exc
    return status, data


@app.post("/v1/messages")
async def v1_messages(request: Request):
    feats = CONFIG["features"]
    try:
        client_req = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")

    if not isinstance(client_req, dict) or "messages" not in client_req:
        return anthropic_error(400, "invalid_request_error", "Missing required field: messages.")

    wants_stream = bool(client_req.get("stream"))
    payload = build_upstream_payload(client_req, feats)
    # Pull the diagnostic breakdown out so it is NOT sent upstream.
    breakdown = payload.pop("_breakdown", {})

    t0 = time.time()

    # STREAMING PATH: open the SSE stream immediately and run the upstream call
    # concurrently with keepalive pings, so the client never sees dead air.
    if wants_stream:
        return StreamingResponse(
            stream_with_keepalive(payload, client_req, feats, t0, breakdown=breakdown),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # NON-STREAMING PATH: wait for the full upstream reply, then return JSON.
    try:
        status, upstream = await resolve_server_tools(payload, feats)
    except Exception as e:
        log_event({"model": payload.get("model"), "stream": wants_stream,
                   "error": f"upstream connection: {e}"})
        return anthropic_error(502, "api_error", f"Upstream connection failed: {e}")

    # CASE A (non-stream): fully-formed assistant body tagged with an error HTTP
    # status -> the content is usable, treat it as success.
    if status >= 400 and isinstance(upstream, dict) \
            and isinstance(upstream.get("content"), list) and upstream.get("content"):
        log_event({"model": payload.get("model"), "stream": wants_stream, "status": status,
                   "note": "usable content on error status -> recovered as success",
                   "recovered_blocks": len(upstream["content"])})
        status = 200

    if status >= 400:
        # LAST-RESORT SALVAGE (non-stream path): recover partial text from a
        # relay body truncated mid-JSON instead of failing the whole turn.
        salvaged = _salvage_truncated(upstream)
        if salvaged:
            log_event({"model": payload.get("model"), "stream": wants_stream,
                       "status": 200, "note": "salvaged truncated upstream body",
                       "salvaged_chars": len(salvaged)})
            return JSONResponse(status_code=200, content={
                "id": "msg_" + uuid.uuid4().hex[:16], "type": "message",
                "role": "assistant",
                "model": payload.get("model") or CONFIG["model"],
                "content": [{"type": "text", "text": salvaged}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}})
        # Wrap upstream errors in Anthropic shape, surfacing the RAW reason so
        # the real upstream cause is visible (not a vague "invalid key").
        msg = ""
        if isinstance(upstream, dict):
            msg = (upstream.get("error", {}) or {}).get("message") \
                  or upstream.get("message") \
                  or upstream.get("_raw") \
                  or json.dumps(upstream, ensure_ascii=False)[:500]
        else:
            msg = str(upstream)[:500]
        log_event({"model": payload.get("model"), "stream": wants_stream,
                   "status": status, "error": (msg or "")[:200],
                   "attempts": 5})
        etype = "invalid_request_error" if status == 400 else "api_error"
        return anthropic_error(status, etype,
                               f"Upstream {status} (after 5 attempt(s)): {msg}")

    message = process_upstream_message(upstream, client_req, feats)
    meta = message.pop("_meta", {})
    dt = round(time.time() - t0, 2)

    log_event({
        "model": message.get("model"),
        "stream": wants_stream,
        "status": status,
        "latency_s": dt,
        "client_tools": meta.get("client_tools", 0),
        "tool_use": meta.get("has_tool_use", False),
        "tool_names": meta.get("tool_names", []),
        "tool_categories": meta.get("tool_categories", []),
        "had_thinking": meta.get("had_thinking", False),
        "stop_reason": message.get("stop_reason"),
        "usage": message.get("usage"),
        "breakdown": breakdown,
    })

    # (The streaming path returned earlier via stream_with_keepalive.)
    return JSONResponse(content=message)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        req = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Body is not valid JSON.")
    total = 0
    total += approx_tokens(extract_system_text(req.get("system")))
    for m in req.get("messages", []):
        total += approx_tokens(flatten_content_blocks(m.get("content"), m.get("role", "user")))
    for t in req.get("tools") or []:
        total += approx_tokens(json.dumps(t, ensure_ascii=False))
    return JSONResponse(content={"input_tokens": total})


@app.get("/v1/models")
async def models():
    url = CONFIG["upstream_base_url"].rstrip("/") + "/models"
    headers = {"x-api-key": CONFIG["api_key"], "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers)
            return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        # fallback to configured model
        return JSONResponse(content={
            "data": [{"type": "model", "id": CONFIG["model"],
                      "display_name": CONFIG["model"]}]
        })


# --------------------------------------------------------------------------- #
# Admin / dashboard API
# --------------------------------------------------------------------------- #

def masked_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 10:
        return "*" * len(k)
    return k[:7] + "..." + k[-4:]


@app.get("/admin/config")
async def get_admin_config():
    c = json.loads(json.dumps(CONFIG))
    c["api_key_masked"] = masked_key(c.get("api_key", ""))
    c.pop("api_key", None)
    # expose fallback keys masked, so they can be shown but not leaked in full
    c["fallback_api_keys_masked"] = [masked_key(k) for k in (c.get("fallback_api_keys") or [])]
    c.pop("fallback_api_keys", None)
    # backup gateway key: expose masked only, never the full secret
    c["backup_api_key_masked"] = masked_key(c.get("backup_api_key", ""))
    c.pop("backup_api_key", None)
    return JSONResponse(content=c)


@app.post("/admin/config")
async def set_admin_config(request: Request):
    global CONFIG
    body = await request.json()
    with _config_lock:
        feats = body.get("features", {})
        if isinstance(feats, dict):
            CONFIG["features"].update(feats)
        for k in ("upstream_base_url", "model", "ui_lang", "upstream_timeout_s",
                  "backup_enabled", "backup_base_url", "backup_model"):
            if k in body and body[k] is not None:
                CONFIG[k] = body[k]
        # only overwrite api key if a non-masked value is provided
        newkey = body.get("api_key")
        if newkey and "..." not in newkey:
            CONFIG["api_key"] = newkey
        # backup gateway key: same masked-keep rule as the primary key
        bkey = body.get("backup_api_key")
        if bkey is not None and "..." not in bkey:
            CONFIG["backup_api_key"] = bkey
        # fallback keys: replace the whole list only if a clean (non-masked)
        # list is sent. Masked entries contain "..." -> means "keep current".
        fb = body.get("fallback_api_keys")
        if isinstance(fb, list) and not any("..." in str(k) for k in fb):
            CONFIG["fallback_api_keys"] = [str(k).strip() for k in fb if str(k).strip()]
        save_config(CONFIG)
    return await get_admin_config()


@app.get("/admin/logs")
async def get_logs():
    with _log_lock:
        return JSONResponse(content={"logs": list(LOG)})


@app.post("/admin/logs/clear")
async def clear_logs():
    with _log_lock:
        LOG.clear()
    return JSONResponse(content={"ok": True})


@app.post("/admin/shutdown")
async def admin_shutdown():
    """Gracefully stop the proxy process (so the user can stop it from the
    dashboard without opening a terminal). We schedule the exit shortly after
    responding so the HTTP reply reaches the browser first."""
    import threading
    import os
    import signal as _signal

    def _stop():
        time.sleep(0.4)
        # 1) Preferred: ask uvicorn to exit its serve loop gracefully.
        try:
            if _SERVER is not None:
                _SERVER.should_exit = True
                _SERVER.force_exit = True
        except Exception:
            pass
        # 2) Also send a signal (covers the uvicorn.run / reload cases).
        try:
            os.kill(os.getpid(), getattr(_signal, "SIGTERM", _signal.SIGINT))
        except Exception:
            pass
        # 3) Hard guarantee: if the loop is still alive after a grace period,
        #    force the process down so the button ALWAYS works.
        time.sleep(1.2)
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return JSONResponse(content={"ok": True, "message": "Proxy is shutting down"})


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# Dashboard HTML is defined in a separate constant appended below.
from dashboard_html import DASHBOARD_HTML  # noqa: E402


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8181))
    print(f"JDW Fix Proxy listening on http://{host}:{port}")
    print(f"  Anthropic endpoint : http://{host}:{port}/v1")
    print(f"  Dashboard          : http://{host}:{port}/")
    print(f"  Upstream           : {CONFIG['upstream_base_url']}")
    # Build the Server explicitly (instead of uvicorn.run) so /admin/shutdown
    # can flip server.should_exit for a clean graceful stop.
    _cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
    _SERVER = uvicorn.Server(_cfg)
    globals()["_SERVER"] = _SERVER  # expose to /admin/shutdown
    _SERVER.run()
