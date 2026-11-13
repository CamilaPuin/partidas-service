# TIMED flow architecture

The TIMED endpoints keep their existing FastAPI routes, request and response
models, status codes, and error mapping. The router composes application use
cases from the Redis match repository, the existing HTTP service clients, and
system clock / UUID adapters.

```text
FastAPI router
    -> CreateTimedMatch / FinishTimedMatch
        -> application ports
            <- MatchRepository (Redis)
            <- CasesHttpAdapter (existing Eureka/service client)
            <- UsersHttpAdapter (existing Eureka/service client)
            <- SystemClock / UuidMatchIdGenerator
        -> pure TIMED domain rules
```

The application owns the port protocols. Domain and application modules do not
depend on FastAPI, Pydantic, Redis, Eureka, HTTP clients, or repository modules.
CLASSIC and VERSUS remain in `MatchService`; TIMED stays in its P9 use cases.

TIMED result registration sends its actual `matchId` to `usuarios-service` and
does not send a case difficulty. The use case uses the returned `xpDelta` for
the public result and Redis ranking. The current Users implementation applies
the supplied XP unchanged for non-CLASSIC modes, so its TIMED result matches
the already-persisted calculated result. If a future Users implementation
returns a different TIMED value, the existing `finalResult` stored before the
HTTP call is not reconciled; that behavior remains a documented limitation.

## Existing partial failure after Redis completion

Finishing a TIMED match first validates the solution, then atomically changes
the Redis match from `IN_PROGRESS` to `FINISHED` using the existing `WATCH`
transaction and TTL. Only after that succeeds does it register the result in
`usuarios-service`, update the Redis ranking using the XP it confirms, and
write user history.

If `usuarios-service` fails after Redis has marked the match `FINISHED`, the
match remains closed while its user result, ranking update, and history entry
may be missing. A retry is rejected as an already-finished match and does not
repair those effects. This behavior is documented for follow-up work; P9-B
does not change it or add retries / cross-service transactions.
