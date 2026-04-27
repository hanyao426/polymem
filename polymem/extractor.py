"""LLM-based observation extractor.

Borrows claude-mem v10.6.2's proven prompt structure from modes/code.json:
  - 6 observation types: bugfix, feature, refactor, change, discovery, decision
  - 7 concept tags: how-it-works, why-it-exists, what-changed,
                    problem-solution, gotcha, pattern, trade-off
  - Strict XML output format
  - Observer-role framing ("you are observing a different session")

Cross-model dispatch: Anthropic / OpenAI / Gemini / Ollama (local) / OpenRouter.
Defaults to OpenRouter free tier to keep the system zero-cost by default.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib import request, error

MODES_DIR = Path(__file__).parent / "modes"


@dataclass
class LLMConfig:
    # Default to claude_cli — uses the user's existing Claude Code subscription,
    # no API key needed. Override via POLYMEM_PROVIDER for other backends.
    provider: str = os.getenv("POLYMEM_PROVIDER", "claude_cli")
    model: str = os.getenv("POLYMEM_MODEL", "claude-haiku-4-5-20251001")
    endpoint: str = os.getenv("POLYMEM_ENDPOINT", "")
    api_key: str = os.getenv("POLYMEM_API_KEY", "")
    claude_cli_path: str = os.getenv("POLYMEM_CLAUDE_CLI", "claude")

    def resolve_endpoint(self) -> str:
        if self.endpoint:
            return self.endpoint
        return {
            "anthropic": "https://api.anthropic.com/v1/messages",
            "openai": "https://api.openai.com/v1/chat/completions",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
            "ollama": "http://localhost:11434/api/chat",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        }[self.provider]


def load_mode(mode_name: str = "code") -> dict[str, Any]:
    path = MODES_DIR / f"{mode_name}.json"
    with open(path) as f:
        return json.load(f)


# ─── Prompt assembly (claude-mem pattern) ──────────────────────────────────


def build_extraction_prompt(mode: dict[str, Any]) -> str:
    """Build the system prompt from mode definition."""
    p = mode["prompts"]
    parts = [
        p["system_identity"],
        "",
        p["spatial_awareness"],
        "",
        p["observer_role"],
        "",
        p["recording_focus"],
        "",
        p["skip_guidance"],
        "",
        p["type_guidance"],
        "",
        p["concept_guidance"],
        "",
        p["field_guidance"],
        "",
        p["output_format_header"],
        _xml_template(p),
        "",
        p["footer"],
    ]
    return "\n".join(parts)


def _xml_template(p: dict[str, str]) -> str:
    return f"""
<observation>
  <type>[EXACTLY one of: bugfix|feature|refactor|change|discovery|decision]</type>
  <title>{p['xml_title_placeholder']}</title>
  <subtitle>{p['xml_subtitle_placeholder']}</subtitle>
  <narrative>{p['xml_narrative_placeholder']}</narrative>
  <facts>
    <fact>{p['xml_fact_placeholder']}</fact>
  </facts>
  <concepts>
    <concept>{p['xml_concept_placeholder']}</concept>
  </concepts>
  <files_read>
    <file>{p['xml_file_placeholder']}</file>
  </files_read>
  <files_modified>
    <file>{p['xml_file_placeholder']}</file>
  </files_modified>
</observation>
""".strip()


def build_summary_prompt(mode: dict[str, Any]) -> str:
    p = mode["prompts"]
    return "\n".join([
        p["summary_instruction"],
        "",
        p["summary_format_instruction"],
        f"""
<summary>
  <request>{p['xml_summary_request_placeholder']}</request>
  <investigated>{p['xml_summary_investigated_placeholder']}</investigated>
  <learned>{p['xml_summary_learned_placeholder']}</learned>
  <completed>{p['xml_summary_completed_placeholder']}</completed>
  <next_steps>{p['xml_summary_next_steps_placeholder']}</next_steps>
  <notes>{p['xml_summary_notes_placeholder']}</notes>
</summary>
""".strip(),
        "",
        p["summary_footer"],
    ])


# ─── XML parser ────────────────────────────────────────────────────────────


def parse_observation_xml(xml_text: str) -> Optional[dict[str, Any]]:
    """Parse a single <observation> block."""
    m = re.search(r"<observation>(.*?)</observation>", xml_text, re.DOTALL)
    if not m:
        return None
    body = m.group(1)

    def extract(tag: str) -> Optional[str]:
        mm = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.DOTALL)
        return mm.group(1).strip() if mm else None

    def extract_list(container: str, item: str) -> list[str]:
        cm = re.search(rf"<{container}>(.*?)</{container}>", body, re.DOTALL)
        if not cm:
            return []
        return [m.group(1).strip() for m in re.finditer(rf"<{item}>(.*?)</{item}>", cm.group(1), re.DOTALL)]

    obs = {
        "type": extract("type"),
        "title": extract("title"),
        "subtitle": extract("subtitle"),
        "narrative": extract("narrative"),
        "facts": extract_list("facts", "fact"),
        "concepts": extract_list("concepts", "concept"),
        "files_read": extract_list("files_read", "file"),
        "files_modified": extract_list("files_modified", "file"),
    }
    if obs["type"] not in {"bugfix", "feature", "refactor", "change", "discovery", "decision"}:
        return None
    return obs


def parse_summary_xml(xml_text: str) -> Optional[dict[str, Any]]:
    m = re.search(r"<summary>(.*?)</summary>", xml_text, re.DOTALL)
    if not m:
        return None
    body = m.group(1)

    def extract(tag: str) -> Optional[str]:
        mm = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.DOTALL)
        return mm.group(1).strip() if mm else None

    return {
        "request": extract("request"),
        "investigated": extract("investigated"),
        "learned": extract("learned"),
        "completed": extract("completed"),
        "next_steps": extract("next_steps"),
        "notes": extract("notes"),
    }


# ─── Cross-model LLM dispatcher ────────────────────────────────────────────


def call_llm(cfg: LLMConfig, system: str, user: str, max_tokens: int = 2000) -> tuple[str, int]:
    """Call any supported LLM. Returns (text, token_usage)."""

    # ─── claude CLI path (zero-API-key option) ────────────────────────────
    # Shells out to the Claude Code CLI. Uses the user's existing subscription.
    #   --tools ""                  : disable all tools (no PostToolUse recursion)
    #   --disable-slash-commands    : skills can't interfere with extraction
    #   --no-session-persistence    : extraction sessions don't pollute /resume picker
    #   --system-prompt             : pass extraction rubric directly
    if cfg.provider == "claude_cli":
        cmd = [
            cfg.claude_cli_path,
            "-p",
            "--model", cfg.model,
            "--output-format", "json",
            "--tools", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt", system,
        ]
        proc = subprocess.run(
            cmd,
            input=user,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed (code {proc.returncode}): {proc.stderr[:500]}")
        try:
            data = json.loads(proc.stdout)
            if data.get("is_error"):
                raise RuntimeError(f"claude CLI returned error: {data.get('result', 'unknown')}")
            text = data.get("result", "")
            usage = data.get("usage", {})
            tokens = (
                usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
        except json.JSONDecodeError:
            text = proc.stdout
            tokens = 0
        return text, tokens

    headers = {"Content-Type": "application/json"}
    endpoint = cfg.resolve_endpoint()

    if cfg.provider == "anthropic":
        if cfg.api_key:
            headers["x-api-key"] = cfg.api_key
            headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": cfg.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    elif cfg.provider == "ollama":
        body = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
    else:  # openai / openrouter / gemini-openai-compatible
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        body = {
            "model": cfg.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    req = request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except error.HTTPError as e:
        raise RuntimeError(f"LLM call failed: {e.code} {e.read().decode()}") from e

    # Response parsing
    if cfg.provider == "anthropic":
        text = data["content"][0]["text"]
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    elif cfg.provider == "ollama":
        text = data["message"]["content"]
        tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
    else:
        text = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)

    return text, tokens


# ─── Public API ────────────────────────────────────────────────────────────


def extract_observation(
    tool_name: str,
    tool_input: str,
    tool_response: str,
    cwd: Optional[str] = None,
    cfg: Optional[LLMConfig] = None,
) -> tuple[Optional[dict[str, Any]], int]:
    """Extract a structured observation from a tool-call event."""
    cfg = cfg or LLMConfig()
    mode = load_mode("code")
    system = build_extraction_prompt(mode)

    user_parts = [
        f"<observed_from_primary_session>",
        f"<tool_cwd>{cwd or 'unknown'}</tool_cwd>",
        f"<tool_name>{tool_name}</tool_name>",
        f"<tool_input>{_truncate(tool_input, 8000)}</tool_input>",
        f"<tool_response>{_truncate(tool_response, 8000)}</tool_response>",
        f"</observed_from_primary_session>",
    ]
    user = "\n".join(user_parts)

    text, tokens = call_llm(cfg, system, user)
    obs = parse_observation_xml(text)
    return obs, tokens


def extract_summary(
    last_user_message: str,
    last_assistant_message: str,
    cfg: Optional[LLMConfig] = None,
) -> tuple[Optional[dict[str, Any]], int]:
    cfg = cfg or LLMConfig()
    mode = load_mode("code")
    system = build_summary_prompt(mode)

    user = f"<user_request>{_truncate(last_user_message, 4000)}</user_request>\n\n<assistant_response>{_truncate(last_assistant_message, 12000)}</assistant_response>"

    text, tokens = call_llm(cfg, system, user)
    summary = parse_summary_xml(text)
    return summary, tokens


def _truncate(s: Optional[str], limit: int) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: limit // 2] + f"\n...[truncated {len(s) - limit} chars]...\n" + s[-limit // 2 :]
