"""
test_client.py
===============
Demonstrates the two required test inputs against a running instance of
the API (default http://localhost:8000).

Run the server first:
    uvicorn app.main:app --reload --port 8000

Then in another terminal:
    python test_client.py
"""
import json

import httpx

BASE_URL = "http://localhost:8000"

TEST_1_STANDARD = (
    "Create a project plan for migrating our customer database from "
    "on-premise MySQL to AWS RDS PostgreSQL, for a team of 4 engineers "
    "over 6 weeks, to present to engineering leadership."
)

TEST_2_COMPLEX = (
    "We need something for the board about the new product thing, it's "
    "kind of urgent, include numbers and a timeline but I don't have "
    "exact figures yet, just make it look credible. Also not sure if "
    "it's a proposal or a report, you decide."
)


def run_test(label: str, request_text: str) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(f"Request: {request_text}\n")
    resp = httpx.post(f"{BASE_URL}/agent", json={"request": request_text}, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()

    print(f"Status: {data['status']}")
    print(f"Document type: {data['document_type']}")
    print(f"Document title: {data['document_title']}")
    print(f"Summary: {data['summary']}\n")

    print("Agent's autonomous TODO list:")
    for i, task in enumerate(data["task_list"], 1):
        print(f"  {i}. {task}")

    print("\nAssumptions made by the agent:")
    for a in data["assumptions"]:
        print(f"  - {a}")

    print("\nStep-by-step execution results:")
    for r in data["task_results"]:
        print(f"  [{r['status'].upper():9}] {r['task']}  (provider: {r.get('provider_used')})")

    print(f"\nImprovement applied: {data['improvement_applied']}")
    print(f"\nDownload: {BASE_URL}{data['download_url']}")
    print(f"Saved server-side at: {data['document_path']}")


if __name__ == "__main__":
    run_test("TEST 1: Standard business request", TEST_1_STANDARD)
    run_test("TEST 2: Complex / ambiguous request", TEST_2_COMPLEX)
