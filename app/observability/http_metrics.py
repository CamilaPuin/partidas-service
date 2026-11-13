import time

from prometheus_client import CollectorRegistry, Histogram


APPLICATION = "partidas-service"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
REGISTRY = CollectorRegistry()
HTTP_REQUEST_DURATION = Histogram(
    "http_server_requests_seconds",
    "HTTP server request duration in seconds",
    labelnames=("application", "method", "uri", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)


class HttpRequestMetricsMiddleware:
    """Observe completed HTTP responses using route templates, never raw paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "OTHER").upper()
        path = scope.get("path", "")
        if path == "/metrics":
            await self.app(scope, receive, send)
            return

        normalized_method = method if method in HTTP_METHODS else "OTHER"
        status_code = 500
        started_at = time.perf_counter()

        async def capture_response_start(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_response_start)
        except BaseException:
            status_code = 500
            raise
        finally:
            route = scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            HTTP_REQUEST_DURATION.labels(
                application=APPLICATION,
                method=normalized_method,
                uri=route_template,
                status=str(status_code),
            ).observe(max(0.0, time.perf_counter() - started_at))
