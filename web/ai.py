"""Claude calls for evaluating fit and tailoring application documents."""
from __future__ import annotations

import json
import os

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")

SYSTEM = (
    "You are a careful career coach helping one candidate apply for jobs. "
    "Use only facts present in the candidate's profile; never invent employers, degrees, "
    "skills, dates or numbers. Be honest about gaps. Write in plain, direct language."
)

_client: anthropic.Anthropic | None = None


class AIError(Exception):
    pass


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _ask(prompt: str, schema: dict) -> dict:
    try:
        resp = client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.RateLimitError:
        raise AIError("AI service is busy. Try again in a minute; no credits were used.")
    except anthropic.APIStatusError as e:
        raise AIError(f"AI service error ({e.status_code}); no credits were used.")
    except anthropic.APIConnectionError:
        raise AIError("Could not reach the AI service; no credits were used.")
    if resp.stop_reason == "refusal":
        raise AIError("The AI declined this request; no credits were used.")
    if resp.stop_reason == "max_tokens":
        raise AIError("Response was cut off; no credits were used.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AIError("AI returned an unreadable answer; no credits were used.")


def _context(profile, company, role, posting):
    return (
        f"<candidate_profile>\n{profile}\n</candidate_profile>\n\n"
        f"<job company=\"{company}\" role=\"{role}\">\n{posting}\n</job>\n\n"
    )


def evaluate(profile, company, role, posting) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "fit_score": {"type": "integer", "description": "0-100"},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "gaps": {"type": "array", "items": {"type": "string"}},
            "recommendation": {"type": "string", "enum": ["apply", "apply_with_caveats", "skip"]},
            "summary_md": {"type": "string"},
        },
        "required": ["fit_score", "strengths", "gaps", "recommendation", "summary_md"],
        "additionalProperties": False,
    }
    out = _ask(
        _context(profile, company, role, posting)
        + "Score how well this candidate fits the job from 0 to 100. List concrete strengths and "
        "gaps tied to the posting's requirements, give a recommendation, and write a short "
        "markdown summary (under 150 words).",
        schema,
    )
    out["fit_score"] = max(0, min(100, int(out["fit_score"])))
    return out


def tailor(profile, company, role, posting) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "cv_bullets": {"type": "string", "description": "Markdown bullet list"},
            "cover_letter": {"type": "string"},
        },
        "required": ["cv_bullets", "cover_letter"],
        "additionalProperties": False,
    }
    return _ask(
        _context(profile, company, role, posting)
        + "Write (1) 5-8 CV bullet points rewritten from the candidate's real experience to match "
        "this posting, as a markdown list, and (2) a cover letter under 350 words addressed to "
        f"{company}, specific to the {role} role, with no clichés.",
        schema,
    )
