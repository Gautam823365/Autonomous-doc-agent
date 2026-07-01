"""
Pydantic schemas shared across the agent.

Keeping these in one place gives every module (planner, executor,
doc_generator, API layer) a single source of truth for the shapes of
data flowing through the pipeline -- which matters a lot once an LLM
is involved, since its raw output is untrusted JSON that needs a hard
contract to be validated against.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=3, description="Natural language request from the user")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RECOVERED = "recovered"  # failed once, succeeded via retry/fallback


class PlannedSection(BaseModel):
    """One section of the target document, as decided by the planner."""
    id: str
    title: str
    key_points: List[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    """The agent's own TODO list -- produced autonomously from the request."""
    document_type: str
    document_title: str
    audience: Optional[str] = None
    tone: Optional[str] = "professional"
    assumptions: List[str] = Field(default_factory=list)
    sections: List[PlannedSection]
    tasks: List[str] = Field(default_factory=list)  # human-readable TODO list


class TaskResult(BaseModel):
    task: str
    status: TaskStatus
    detail: Optional[str] = None
    provider_used: Optional[str] = None  # which LLM/fallback actually produced this


class SectionContent(BaseModel):
    id: str
    title: str
    paragraphs: List[str]


class AgentResponse(BaseModel):
    status: str
    summary: str
    document_type: str
    document_title: str
    assumptions: List[str]
    task_list: List[str]
    task_results: List[TaskResult]
    improvement_applied: str
    document_path: str
    download_url: str
