from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, HTTPException
from py_eureka_client.eureka_client import EurekaClient

from app.core.redis_client import redis_client
from app.routers.matches import router as matches_router

EUREKA_SERVER = "https://eureka-server-1-ngbb.onrender.com"
EUREKA_INSTANCE_HOST = "partidas-service"
PORT = 8000


@asynccontextmanager
async def lifespan(app: FastAPI):
    eureka_client = EurekaClient(
        eureka_server=EUREKA_SERVER,
        app_name="partidas-service",
        instance_host=EUREKA_INSTANCE_HOST,
        instance_port=PORT,
    )
    await eureka_client.start()
    try:
        yield
    finally:
        await eureka_client.stop()


app = FastAPI(title="Partidas Service", lifespan=lifespan)
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
