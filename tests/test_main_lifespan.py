import asyncio
from types import SimpleNamespace

from app import main


def test_lifespan_starts_and_stops_kafka_with_eureka(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET", "test-only-gateway-secret")
    monkeypatch.setenv("METRICS_SCRAPE_PASSWORD", "test-only-scrape-password")
    monkeypatch.setenv("EUREKA_SERVER", "http://eureka-test:8762/eureka/")
    monkeypatch.setattr(main, "EUREKA_SERVER", "http://eureka-test:8762/eureka/")
    events = []

    class FakeEureka:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            events.append("eureka-start")

        async def stop(self):
            events.append("eureka-stop")

    class FakeKafka:
        async def start(self):
            events.append("kafka-start")

        async def stop(self):
            events.append("kafka-stop")

    class FakeSaga:
        def __init__(self, _repository):
            pass

        async def start(self):
            events.append("saga-start")

        async def stop(self):
            events.append("saga-stop")

    class FakeReconciler:
        def __init__(self, _repository):
            pass

        async def start(self):
            events.append("reconciler-start")

        async def stop(self):
            events.append("reconciler-stop")

    monkeypatch.setattr(main, "EurekaClient", FakeEureka)
    monkeypatch.setattr(main, "KafkaPingRuntime", FakeKafka)
    monkeypatch.setattr(main, "MatchSagaRuntime", FakeSaga)
    monkeypatch.setattr(main, "SagaReconciler", FakeReconciler)

    async def run_lifespan():
        async with main.lifespan(SimpleNamespace()):
            events.append("serving")

    asyncio.run(run_lifespan())

    assert events == [
        "eureka-start",
        "kafka-start",
        "saga-start",
        "reconciler-start",
        "serving",
        "reconciler-stop",
        "saga-stop",
        "kafka-stop",
        "eureka-stop",
    ]
