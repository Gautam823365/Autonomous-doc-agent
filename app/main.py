"""
main.py
=======
FastAPI entrypoint. Exposes POST /agent which runs the full autonomous
pipeline:

    request (NL text)
        -> planner.create_plan()      [autonomous task/TODO list + section plan]
        -> executor.execute_plan()    [executes each planned step, with recovery]
        -> doc_generator.build_docx() [renders the final .docx]
        -> AgentResponse              [plan, task results, assumptions, file link]

Run with:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .doc_generator import build_docx
from .executor import execute_plan
from .llm_client import LLMClient
from .models import AgentRequest, AgentResponse, TaskResult, TaskStatus
from .planner import create_plan

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent.api")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "generated_docs")

app = FastAPI(
    title="Autonomous Document Agent",
    description="Accepts a natural-language request, plans its own tasks, "
    "executes them, and returns a generated Word document.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

llm_client = LLMClient()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/agent", response_model=AgentResponse)
async def run_agent(payload: AgentRequest) -> AgentResponse:
    """
    --- Request validation & guardrails (lightweight) ---
    Pydantic already enforces a non-trivial request string. We add one
    more guardrail here: reject requests that are far too short/vague
    to plan against (e.g. "hi", "doc") with a clear 422 rather than
    silently generating a meaningless document.
    """
    request_text = payload.request.strip()
    if len(request_text.split()) < 3:
        raise HTTPException(
            status_code=422,
            detail="Request is too short for the agent to plan against. "
            "Please describe what document you need in a sentence or two.",
        )

    started = time.time()
    logger.info("Received agent request: %r", request_text)

    task_results: list[TaskResult] = []

    # --- Step 1: Autonomous planning ---
    task_results.append(TaskResult(task="Analyze request and build execution plan", status=TaskStatus.RUNNING))
    plan, plan_provider = await create_plan(llm_client, request_text)
    task_results[-1] = TaskResult(
        task="Analyze request and build execution plan",
        status=TaskStatus.DONE if plan_provider != "fallback_template" else TaskStatus.RECOVERED,
        provider_used=plan_provider,
        detail=f"Selected document type '{plan.document_type}' with {len(plan.sections)} sections.",
    )

    # --- Step 2: Execute each planned section ---
    try:
        section_contents, section_task_results = await execute_plan(llm_client, plan, request_text)
        task_results.extend(section_task_results)
    except Exception as exc:  # noqa: BLE001 - guardrail of last resort
        logger.exception("Unrecoverable failure during plan execution")
        raise HTTPException(status_code=500, detail=f"Agent failed during execution: {exc}") from exc

    # --- Step 3: Render the Word document ---
    task_results.append(TaskResult(task="Render final .docx document", status=TaskStatus.RUNNING))
    try:
        doc_path = build_docx(plan, section_contents, OUTPUT_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Document rendering failed")
        task_results[-1] = TaskResult(
            task="Render final .docx document", status=TaskStatus.FAILED, detail=str(exc)
        )
        raise HTTPException(status_code=500, detail=f"Document rendering failed: {exc}") from exc
    task_results[-1] = TaskResult(task="Render final .docx document", status=TaskStatus.DONE)

    elapsed = time.time() - started
    filename = os.path.basename(doc_path)
    recovered = any(r.status == TaskStatus.RECOVERED for r in task_results)

    summary = (
        f"Generated a {plan.document_type} ('{plan.document_title}') with "
        f"{len(plan.sections)} sections in {elapsed:.1f}s."
    )
    if recovered:
        summary += " Note: one or more steps fell back to degraded/local generation due to LLM provider issues."

    logger.info(summary)

    return AgentResponse(
        status="completed",
        summary=summary,
        document_type=plan.document_type,
        document_title=plan.document_title,
        assumptions=plan.assumptions,
        task_list=plan.tasks,
        task_results=task_results,
        improvement_applied=(
            "Retry & Fallback Logic: each LLM call is retried with exponential backoff, "
            "then fails over Groq -> local Ollama -> deterministic template generator, "
            "so the agent always returns a complete document even when LLM providers are "
            "unavailable, rate-limited, or erroring."
        ),
        document_path=doc_path,
        download_url=f"/download/{filename}",
    )


@app.get("/download/{filename}")
async def download(filename: str) -> FileResponse:
    safe_name = os.path.basename(filename)  # guardrail: prevent path traversal
    path = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name,
    )
