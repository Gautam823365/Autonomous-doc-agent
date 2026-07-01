"""
planner.py
==========
Turns a free-text request into an ExecutionPlan: a document type, a list
of sections the final .docx should contain, and a human-readable TODO
list -- all decided autonomously by the agent, not hardcoded per request.

For ambiguous/underspecified requests, the planner is explicitly told to
make reasonable assumptions and *log* them (rather than blocking on a
clarifying question, since this is a one-shot autonomous API) so the
final document and API response both surface what was assumed.
"""
from __future__ import annotations

import logging
from typing import List

from .llm_client import LLMClient, LLMAllProvidersFailed
from .models import ExecutionPlan, PlannedSection

logger = logging.getLogger("agent.planner")

PLANNER_SYSTEM_PROMPT = """You are the planning module of an autonomous business-document agent.
Given a user's natural-language request, decide:
1. What KIND of business document best satisfies it (proposal, meeting minutes,
   project plan, business report, technical design, SOP, product spec, etc.)
2. A short, specific title for the document.
3. The target audience and an appropriate tone.
4. Any assumptions you must make because the request is ambiguous, incomplete,
   or self-contradictory (e.g. missing dates, budget, names, scope). Never
   refuse or ask a clarifying question -- always make the most reasonable
   professional assumption and record it explicitly.
5. A logical list of sections the document should contain (typically 4-8).
6. A plain-English TODO/task list describing the steps you (the agent) will
   execute to produce the final document -- this is shown to the user as
   proof of your planning.

Respond with ONLY a JSON object, no prose, no markdown fences, matching exactly:
{
  "document_type": "string",
  "document_title": "string",
  "audience": "string",
  "tone": "string",
  "assumptions": ["string", ...],
  "tasks": ["string", ...],
  "sections": [
    {"id": "slug-id", "title": "Section Title", "key_points": ["point 1", "point 2"]}
  ]
}
"""


# Lightweight keyword -> document-type templates used when every LLM
# provider is unreachable. This keeps the offline fallback "autonomous"
# (it still classifies the request and picks a tailored structure)
# rather than always emitting the same generic report.
_FALLBACK_TEMPLATES = {
    "meeting minutes": (
        "Meeting Minutes",
        ["meeting", "minutes", "attendees", "agenda"],
        [
            ("attendees", "Attendees & Logistics", ["Attendees", "Date/time", "Location"]),
            ("agenda", "Agenda", ["Topics covered"]),
            ("discussion", "Discussion Summary", ["Key points raised", "Decisions made"]),
            ("action-items", "Action Items", ["Owner", "Due date"]),
            ("next-steps", "Next Steps", ["Follow-up meeting", "Open items"]),
        ],
    ),
    "project plan": (
        "Project Plan",
        ["project plan", "migration", "sprint", "milestones", "roadmap"],
        [
            ("overview", "Project Overview", ["Goal", "Background"]),
            ("scope", "Scope & Objectives", ["In scope", "Out of scope", "Success criteria"]),
            ("timeline", "Timeline & Milestones", ["Phases", "Key dates"]),
            ("resources", "Team & Resources", ["Roles", "Owners"]),
            ("risks", "Risks & Mitigations", ["Known risks", "Mitigation plan"]),
        ],
    ),
    "proposal": (
        "Business Proposal",
        ["proposal", "pitch", "offer"],
        [
            ("problem", "Problem Statement", ["Current pain points"]),
            ("solution", "Proposed Solution", ["Approach", "Differentiators"]),
            ("benefits", "Benefits & Impact", ["Value to stakeholders"]),
            ("investment", "Investment & Timeline", ["Cost", "Schedule"]),
            ("next-steps", "Next Steps", ["Decision needed", "Approval process"]),
        ],
    ),
    "sop": (
        "Standard Operating Procedure",
        ["sop", "standard operating procedure", "process document"],
        [
            ("purpose", "Purpose & Scope", ["Why this SOP exists", "Who it applies to"]),
            ("procedure", "Procedure Steps", ["Step-by-step instructions"]),
            ("roles", "Roles & Responsibilities", ["Who does what"]),
            ("exceptions", "Exceptions & Escalation", ["Edge cases", "Escalation path"]),
            ("revision", "Revision History", ["Version", "Change log"]),
        ],
    ),
    "technical design": (
        "Technical Design Document",
        ["technical design", "architecture", "system design"],
        [
            ("context", "Background & Context", ["Problem being solved"]),
            ("design", "Proposed Design", ["Architecture overview", "Key components"]),
            ("alternatives", "Alternatives Considered", ["Tradeoffs"]),
            ("risks", "Risks & Open Questions", ["Unknowns"]),
            ("rollout", "Rollout Plan", ["Phasing", "Rollback plan"]),
        ],
    ),
}

_DEFAULT_TEMPLATE = (
    "Business Report",
    [],
    [
        ("overview", "Overview", ["Purpose", "Context"]),
        ("objectives", "Objectives", ["Goals", "Success criteria"]),
        ("approach", "Approach / Details", ["Plan", "Key steps"]),
        ("timeline", "Timeline & Next Steps", ["Milestones", "Owners"]),
        ("risks", "Risks & Considerations", ["Open questions"]),
    ],
)


def _classify_fallback_type(request: str) -> tuple[str, List[PlannedSection]]:
    lowered = request.lower()
    for doc_type, keywords, section_specs in _FALLBACK_TEMPLATES.values():
        if any(kw in lowered for kw in keywords):
            sections = [PlannedSection(id=i, title=t, key_points=kp) for i, t, kp in section_specs]
            return doc_type, sections
    doc_type, _keywords, section_specs = _DEFAULT_TEMPLATE
    sections = [PlannedSection(id=i, title=t, key_points=kp) for i, t, kp in section_specs]
    return doc_type, sections


def _fallback_plan(request: str) -> ExecutionPlan:
    """Deterministic plan used only if every LLM provider is unreachable.

    Still classifies the request by keyword to pick a tailored section
    structure (meeting minutes vs. project plan vs. proposal, etc.) so
    the agent degrades gracefully rather than going fully generic.
    """
    logger.warning("Using deterministic fallback planner (all LLM providers unavailable)")
    doc_type, sections = _classify_fallback_type(request)
    return ExecutionPlan(
        document_type=doc_type,
        document_title=f"{doc_type}: Generated from Request",
        audience="Internal stakeholders",
        tone="professional",
        assumptions=[
            "LLM providers were unavailable, so the document structure was chosen via "
            "keyword-based classification rather than full language understanding.",
            "Specific figures, names, and dates were not available and are marked as placeholders.",
        ],
        tasks=[
            f"Classify request and select a '{doc_type}' template (LLM unavailable, used keyword match)",
            "Populate standard sections with placeholder professional content",
            "Render the document to .docx",
        ],
        sections=sections,
    )


async def create_plan(llm: LLMClient, request: str) -> tuple[ExecutionPlan, str]:
    """Returns (plan, provider_used). provider_used is 'fallback_template' on degradation."""
    try:
        raw, provider = await llm.complete_json(PLANNER_SYSTEM_PROMPT, f"User request:\n{request}")
        sections = [PlannedSection(**s) for s in raw.get("sections", [])]
        if not sections:
            raise ValueError("Planner returned no sections")
        plan = ExecutionPlan(
            document_type=raw.get("document_type", "Business Report"),
            document_title=raw.get("document_title", "Generated Document"),
            audience=raw.get("audience"),
            tone=raw.get("tone", "professional"),
            assumptions=raw.get("assumptions", []),
            tasks=raw.get("tasks", []),
            sections=sections,
        )
        return plan, provider
    except (LLMAllProvidersFailed, ValueError, KeyError, TypeError) as exc:
        logger.warning("Planning via LLM failed (%s); using fallback plan", exc)
        return _fallback_plan(request), "fallback_template"
