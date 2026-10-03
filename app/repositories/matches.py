import json
from typing import Any, Dict, List, Optional, Tuple

import redis


class MatchRepository:
    def __init__(self, client: redis.Redis):
        self.client = client

    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]:
        raw_match = self.client.get(f"match:{match_id}")
        return json.loads(raw_match) if raw_match else None

    def save_match(self, match_id: str, match_data: Dict[str, Any]) -> None:
        self.client.set(f"match:{match_id}", json.dumps(match_data))

    def add_open_match(self, match_id: str) -> None:
        self.client.sadd("matches:open", match_id)

    def remove_open_match(self, match_id: str) -> None:
        self.client.srem("matches:open", match_id)

    def list_open_match_ids(self) -> set:
        return self.client.smembers("matches:open")

    def save_progress(self, match_id: str, user_id: str, progress: float) -> None:
        self.client.hset(f"match:{match_id}:progress", user_id, progress)

    def get_progress(self, match_id: str, user_id: str) -> float:
        return float(self.client.hget(f"match:{match_id}:progress", user_id) or 0.0)

    def increment_score(self, user_id: str, delta: int) -> None:
        self.client.zincrby("ranking:global", delta, user_id)

    def list_ranking(self, limit: int) -> List[Tuple[str, float]]:
        return self.client.zrevrange(
            "ranking:global",
            0,
            limit - 1,
            withscores=True,
        )

    def save_user_history(self, user_id: str, history_entry: Dict[str, Any]) -> None:
        self.client.lpush(
            f"user:{user_id}:history",
            json.dumps(history_entry),
        )

    def list_user_history(self, user_id: str) -> List[Dict[str, Any]]:
        history_items = self.client.lrange(f"user:{user_id}:history", 0, -1)
        return [json.loads(item) for item in history_items]
