"""Lambda trigger: invokes the dep-update-agent runtime for each configured repo.

Triggered by EventBridge on a weekly schedule. Iterates over the REPOS list and
calls the AgentCore runtime asynchronously for each one.
"""

import json
import os
import hashlib

import boto3

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
REPOS = json.loads(os.environ.get("REPOS", "[]"))
ALLOW_FIXES = os.environ.get("ALLOW_FIXES", "true").lower() == "true"
MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))


def handler(event, context):
    """Invoke the AgentCore runtime once per configured repository.

    Returns a list of invocation results with repo URL and response status.
    """
    client = boto3.client("bedrock-agentcore")
    results = []

    for repo_url in REPOS:
        # Deterministic session ID per repo — must be at least 33 chars
        repo_hash = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
        session_id = f"dep-update-{repo_hash}"

        payload = json.dumps({
            "repo_url": repo_url,
            "allow_fixes": ALLOW_FIXES,
            "max_fix_attempts": MAX_FIX_ATTEMPTS,
        })

        try:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=RUNTIME_ARN,
                runtimeSessionId=session_id,
                payload=payload,
            )
            status_code = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            # Parse the response body if available
            body = {}
            if "payload" in resp:
                try:
                    raw = resp["payload"].read().decode("utf-8") if hasattr(resp["payload"], "read") else str(resp["payload"])
                    body = json.loads(raw)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    body = {"raw": str(resp.get("payload", ""))[:500]}

            results.append({
                "repo": repo_url,
                "status_code": status_code,
                "result": body,
            })
        except Exception as e:
            results.append({
                "repo": repo_url,
                "status_code": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })

    print(json.dumps({
        "event": "dep-update-trigger-complete",
        "repos_processed": len(REPOS),
        "results": results,
    }))

    return results
