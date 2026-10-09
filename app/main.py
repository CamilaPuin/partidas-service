from contextlib import asynccontextmanager
import os

import redis
from fastapi import FastAPI, HTTPException
from py_eureka_client.eureka_client import EurekaClient

from app.core.redis_client import redis_client
from app.routers.matches import router as matches_router

EUREKA_SERVER = os.getenv(
    "EUREKA_SERVER",
    "http://localhost:8762/eureka/",
)

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
EUREKA_INSTANCE_HOST = os.getenv(
    "EUREKA_INSTANCE_HOST",
    RENDER_EXTERNAL_HOSTNAME or "localhost",
)
PORT = int(os.getenv("PORT", "8000"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    is_render = bool(RENDER_EXTERNAL_HOSTNAME)
    public_base_url = (
        f"https://{EUREKA_INSTANCE_HOST}"
        if is_render
        else f"http://{EUREKA_INSTANCE_HOST}:{PORT}"
    )
    
    eureka_client = EurekaClient(
        eureka_server=EUREKA_SERVER,
        app_name="partidas-service",
        instance_host=EUREKA_INSTANCE_HOST,
        instance_port=443 if is_render else PORT,
        instance_unsecure_port_enabled=not is_render,
        instance_secure_port=443 if is_render else None,
        instance_secure_port_enabled=is_render,
        home_page_url=f"{public_base_url}/docs",
        status_page_url=f"{public_base_url}/health",
        health_check_url=f"{public_base_url}/health",
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