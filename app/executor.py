"""
executor.py
===========
Executes the ExecutionPlan step by step: one task per document section.
Each step is independently resilient -- if content generation for a
single section fails even after the LLMClient's own retry/fallover,
the executor recovers by substituting a clearly-labelled placeholder
paragraph for *that section only*, rather than failing the whole
request. This is the same retry/fallback philosophy as llm_client.py,
applied one layer up so a single bad section can't sink the document.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from .llm_client import LLMClient, LLMAllProvidersFailed
from .models import ExecutionPlan, PlannedSection, SectionContent, TaskResult, TaskStatus

logger = logging.getLogger("agent.executor")

SECTION_SYSTEM_PROMPT = """You are the content-writing module of an autonomous business-document agent.
Write the body content for ONE section of a {doc_type} titled "{doc_title}".
Audience: {audience}. Tone: {tone}.
Section: "{section_title}"
Key points to cover: {key_points}

Write 2-4 well-formed professional paragraphs (no headings, no markdown,
no bullet characters -- plain prose paragraphs only, each on its own line).
Use realistic mock data (names, figures, dates) where the request did not
supply real ones, and keep it consistent with a {doc_type}.
Respond with ONLY the paragraphs of text, nothing else."""


def _fallback_section_content(section: PlannedSection) -> List[str]:
    bullets = "; ".join(section.key_points) if section.key_points else "the relevant details"
    return [
        f"[Auto-generated placeholder: the language model was unavailable for this section.] "
        f"This section, '{section.title}', should address {bullets}. "
        f"Please replace this placeholder with finalized content before distribution."
    ]


async def execute_plan(
    llm: LLMClient, plan: ExecutionPlan, original_request: str
) -> Tuple[List[SectionContent], List[TaskResult]]:
    contents: List[SectionContent] = []
    results: List[TaskResult] = []

    for section in plan.sections:
        task_label = f"Generate content for section: '{section.title}'"
        prompt = SECTION_SYSTEM_PROMPT.format(
            doc_type=plan.document_type,
            doc_title=plan.document_title,
            audience=plan.audience or "general business audience",
            tone=plan.tone or "professional",
            section_title=section.title,
            key_points=", ".join(section.key_points) if section.key_points else "use your judgement",
        )
        try:
            result = await llm.complete(
                system=prompt,
                user=f"Original user request for context: {original_request}",
            )
            paragraphs = [p.strip() for p in result.text.split("\n") if p.strip()]
            contents.append(SectionContent(id=section.id, title=section.title, paragraphs=paragraphs))
            results.append(
                TaskResult(task=task_label, status=TaskStatus.DONE, provider_used=result.provider)
            )
        except LLMAllProvidersFailed as exc:
            logger.warning("Section '%s' failed via all LLM providers (%s); using placeholder", section.title, exc)
            contents.append(
                SectionContent(id=section.id, title=section.title, paragraphs=_fallback_section_content(section))
            )
            results.append(
                TaskResult(
                    task=task_label,
                    status=TaskStatus.RECOVERED,
                    detail="All LLM providers failed; substituted placeholder content for this section only.",
                    provider_used="fallback_template",
                )
            )

    return contents, results
