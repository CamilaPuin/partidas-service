from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4


def match_finished_event(
    match_id: str,
    case_id: str,
    mode: str,
    time_seconds: int,
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "eventId": str(uuid4()),
        "correlationId": str(uuid4()),
        "occurredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {
            "matchId": match_id,
            "caseId": case_id,
            "mode": mode,
            "timeSeconds": time_seconds,
            "results": results,
        },
    }
