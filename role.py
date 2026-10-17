import json
import re
from typing import Dict, Any, Optional
from datetime import datetime


async def execute(data: dict, ctx) -> dict:
    # Extract task ID from input text using robust pattern
    text = data.get("text", "")
    task_id_match = re.search(r"(?:任务|ID|id)[\s：:\-]*([A-Za-z]+-\\d+)", text)
    if not task_id_match:
        task_id_match = re.search(r"([A-Za-z]+-\\d+)", text)
    task_id = task_id_match.group(1) if task_id_match else None

    # Validate task ID format
    if not task_id or not re.match(r"^[A-Za-z]+-\\d+$", task_id):
        return {
            "decision": "BLOCKED",
            "reason": "missing_or_invalid_task_id",
            "evidence": {},
            "risk": "invalid_input",
            "next_step": "human_approval_required"
        }

    # Fetch prompt and compile
    prompt = await ctx.prompt("role")
    compiled_prompt = prompt.compile(input=text, task_id=task_id)

    # Step 1: Read devflow job to get task stage & status
    try:
        job_result = await ctx.tools.call("devflow.job.read", {"id": task_id})
        if not isinstance(job_result, dict) or "stage" not in job_result:
            raise ValueError("invalid_job_response")
    except Exception:
        return {
            "decision": "BLOCKED",
            "reason": "job_read_failed",
            "evidence": {"task_id": task_id},
            "risk": "stale_evidence",
            "next_step": "human_approval_required"
        }

    # Step 2: Fetch stage report
    try:
        stage_report = await ctx.tools.call("devflow.stage.report", {"id": task_id})
        if not isinstance(stage_report, dict):
            raise ValueError("invalid_stage_report")
    except Exception:
        return {
            "decision": "BLOCKED",
            "reason": "stage_report_unavailable",
            "evidence": {"task_id": task_id},
            "risk": "stale_evidence",
            "next_step": "human_approval_required"
        }

    # Step 3: Fetch CI log
    try:
        ci_log = await ctx.tools.call("ci.log.fetch", {"id": task_id})
        if not isinstance(ci_log, dict) or "status" not in ci_log:
            raise ValueError("invalid_ci_log")
    except Exception:
        return {
            "decision": "BLOCKED",
            "reason": "ci_log_unavailable",
            "evidence": {"task_id": task_id},
            "risk": "stale_evidence",
            "next_step": "human_approval_required"
        }

    # Check for stale evidence: disallow cached/unknown/undefined values
    stale_indicators = [
        str(job_result.get("stage", "")).lower() in ["undefined", "unknown", "none", ""],
        str(ci_log.get("status", "")).lower() in ["unknown", "cached", "n/a", "pending"],
        "H1" not in str(stage_report.get("approval", "")),
        "H2" not in str(stage_report.get("approval", ""))
    ]
    if any(stale_indicators):
        return {
            "decision": "BLOCKED",
            "reason": "stale_or_incomplete_evidence",
            "evidence": {
                "job_stage": job_result.get("stage"),
                "ci_status": ci_log.get("status"),
                "stage_approval": stage_report.get("approval")
            },
            "risk": "stale_evidence",
            "next_step": "human_approval_required"
        }

    # Gate check: H1 & H2 must be confirmed in stage report
    approvals = str(stage_report.get("approval", ""))
    if "H1" not in approvals or "H2" not in approvals:
        return {
            "decision": "BLOCKED",
            "reason": "gate_not_met",
            "evidence": {"stage_approval": approvals},
            "risk": "gate",
            "next_step": "human_approval_required"
        }

    # CI must be PASS (case-insensitive, no 'cached')
    ci_status_clean = str(ci_log.get("status", "")).strip().lower()
    if not (ci_status_clean == "pass" or ci_status_clean == "✅ pass"):
        return {
            "decision": "BLOCKED",
            "reason": "ci_failed_or_unconfirmed",
            "evidence": {"ci_status": ci_log.get("status")},
            "risk": "stale_evidence",
            "next_step": "human_approval_required"
        }

    # All checks passed → COORDINATE
    # Extract target agent hint from input (if present), but only delegate to known agents
    target_agent_match = re.search(r"委派[\s：:\-]*(\\w+[-\\w]*)", text)
    target_agent = target_agent_match.group(1) if target_agent_match else None

    # Only delegate if agent is registered (mock list of authorized agents)
    authorized_agents = ["qa-tester-202", "release-auditor-301", "security-scan-405"]
    if target_agent and target_agent not in authorized_agents:
        return {
            "decision": "BLOCKED",
            "reason": "unauthorized_delegate_target",
            "evidence": {"requested_agent": target_agent},
            "risk": "authorization",
            "next_step": "human_approval_required"
        }

    # Build output structure per spec
    result = {
        "decision": "COORDINATE",
        "task_stage": job_result.get("stage"),
        "ci_evidence": {
            "status": ci_log.get("status"),
            "log_snippet": (ci_log.get("log", "")[:200] + "...") if len(ci_log.get("log", "")) > 200 else ci_log.get("log", "")
        },
        "gate_conclusion": "PASSED",
        "delegate_target": target_agent if target_agent else None,
        "risks": [],
        "next_step": "human_approval_required"
    }

    # Add risk hints if observed
    if "⚠️" in str(ci_log.get("log", "")) or "warning" in str(ci_log.get("log", "")).lower():
        result["risks"].append("ci_warnings_present")
    if "deprecated" in str(stage_report.get("notes", "")).lower():
        result["risks"].append("deprecated_feature_used")

    # Final guard: reject injection attempts via input sanitization (already done above),
    # and block any command-like or path-write patterns in original text
    dangerous_patterns = [
        r"[;|&`$]",
        r"/dev/null",
        r"/tmp/.*\.txt",
        r"DROP TABLE",
        r"--",
        r"\\x[0-9a-fA-F]{2}",
        r"exec|system|os\\."
    ]
    if any(re.search(p, text) for p in dangerous_patterns):
        return {
            "decision": "BLOCKED",
            "reason": "input_injection_attempt",
            "evidence": {"input_snippet": text[:64]},
            "risk": "injection",
            "next_step": "human_approval_required"
        }

    return result