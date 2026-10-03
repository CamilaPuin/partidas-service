import redis
from fastapi import FastAPI, HTTPException

from app.core.redis_client import redis_client
from app.routers.matches import router as matches_router

app = FastAPI(title="Partidas Service")
app.include_router(matches_router)


@app.get("/health")
def health_check():
    try:
        redis_client.ping()
    except redis.RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail="Redis no está disponible",
        ) from exc
    return {"status": "ok"}
