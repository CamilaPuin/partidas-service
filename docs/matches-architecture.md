# Match flows architecture

CLASSIC, TIMED and VERSUS keep their public routes and DTOs in the FastAPI
router. The router is the composition root: it wires application use cases to
the Redis repository and HTTP adapters for `casos-service` and
`usuarios-service`.

```text
FastAPI routes and DTOs
    -> CLASSIC / VERSUS application service
    -> CreateTimedMatch / FinishTimedMatch
        -> application ports (MatchStore, CasesGateway, UsersGateway, Clock, IDs)
            <- MatchRepository (Redis)
            <- CasesHttpAdapter (Eureka + cases-service HTTP API)
            <- UsersHttpAdapter (Eureka + users-service HTTP API)
            <- SystemClock / UuidMatchIdGenerator
```

The application layer returns plain dictionaries and collections. It does not
import FastAPI, Pydantic, Redis, Eureka, HTTP clients or infrastructure
adapters. `CasesHttpAdapter.validate()` delegates to the real
`POST /cases/{caseId}/validate` operation through the service client; there is
no client-provided `isCorrect` value or production-side simulated validation.
CLASSIC creation also retrieves case difficulty from `casos-service` and user
progression from `usuarios-service` before applying the level gate.

## Duplicate submissions and service errors

- CLASSIC requires an `attemptId`; Redis fingerprints the submitted positions.
  Repeating an identical attempt replays its stored result, while reusing its
  ID with different positions conflicts. Case-service errors release the
  temporary validation reservation. A user-service failure leaves a
  `FINALIZING` attempt that can retry registration without revalidating cases.
- TIMED and VERSUS validate against `casos-service` before changing the match.
  A failed validation call leaves the match available for retry. Atomic Redis
  state transitions allow only one concurrent finish/submission to close a
  match; subsequent submissions are rejected without another validation or
  result registration.
- Service integration failures keep their existing HTTP mapping: missing cases
  return 404, case-service failures return 503, and other upstream failures
  return 502. Invalid solutions retain each mode's existing response contract.

Timed and versus completion still span Redis and `usuarios-service`, which do
not share a transaction. If users-service fails after Redis marks a match
finished, the user result, ranking or history may be incomplete. The failure is
propagated and covered by tests; this change does not add a distributed
transaction or background repair process.
