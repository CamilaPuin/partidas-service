# H04-05 — Reconciliación y recuperación de sagas pendientes

## A. Estado inicial

- Repositorio: `partidas-service`.
- Rama de trabajo: `fix/h04-05-saga-reconciliation`.
- Rama base verificada antes de crearla: `main` en `0afa0d62de2cab836c1fb0f27ab8d67e3b34ce24`; `origin/main` coincidía y el árbol estaba limpio.
- El PR #15 (`H04-04`) estaba cerrado y fusionado. Se comprobó que el merge `8b019487208796adcd2c406d36b4673311c63ed8` era ancestro de `main`. El remoto y el checkout no se actualizaron durante este trabajo.
- En el estado inicial H04-05 no había commits ni publicación de cambios.

## B. Diagnóstico

El cierre de CLASSIC, TIMED y VERSUS ya persiste el estado terminal junto con `matchFinishedEvent` y su publicación durable en `outbox:matches.finished.v1`, usando la transacción Redis del cierre. El publicador mueve los eventos a `inflight`, espera el ACK Kafka y luego los elimina; el arranque recupera los elementos que quedaron en `inflight`.

Users-service procesa `matches.finished.v1` dentro de una transacción PostgreSQL: registra resultados y recompensas, y encola la confirmación en su outbox en la misma transacción. El registro se deduplica por usuario/partida; el outbox por `sourceEventId` y fingerprint. Si el mismo evento original vuelve a llegar después de que la confirmación fue publicada, Users vuelve a dejar su confirmación persistida en `PENDING`, preservando el `eventId` y payload de esa confirmación. Estas conclusiones se obtuvieron leyendo `users_service`; ese repositorio no fue modificado.

Partidas aplica la confirmación con Lua: verifica el estado terminal y los resultados almacenados, y actualiza saga, ranking e historial atómicamente. La transición terminal también retira la saga de los índices de reconciliación.

## C. Matriz de recuperabilidad

| Caso | Datos y fuente de verdad | Recuperación automática | Acción segura y límite |
|---|---|---|---|
| A. Evento aún en outbox Redis | Evento original persistido en Redis | Sí, el publicador normal lo entrega | Esperar/reintentar por el publicador; el reconciliador no necesita duplicarlo mientras no supere el umbral y el backoff. |
| B. Evento en inflight y reinicio | Evento persistido en inflight | Sí | `recover_inflight_match_events()` lo devuelve al outbox al reiniciar. Puede haber entrega Kafka repetida; Users deduplica por el `eventId` original. |
| C. Kafka recibió el evento y Partidas desconoce el resultado de Users | Partida terminal y evento original conservados en Redis; Kafka/Users son la autoridad sobre el progreso del otro lado | Sí, de forma idempotente, mientras Redis conserve el registro | Tras edad mínima se reencola el mismo evento por el outbox Redis. No se declara fallo por timeout ni se inventa una confirmación. |
| D. PostgreSQL registró resultado, confirmación aún pendiente en outbox Users | Resultado y outbox de Users en una transacción | Sí, por el publicador de Users | Normalmente no requiere acción de Partidas; reemitir el mismo evento es seguro según la deduplicación observada. |
| E. Confirmación Kafka publicada, Partidas falla antes de aplicarla | Evento Kafka puede volver a entregarse; la partida terminal continúa en Redis | Sí, sujeto a retención Kafka/DLQ | H04-04 conserva el offset hasta completar Redis; redelivery aplica la transición Lua idempotente. La reemisión desde Partidas es una recuperación adicional si sigue pendiente. |
| F. Redis pierde partida y outbox | No hay copia consultable de `matchFinishedEvent` en Partidas | No | Irrecuperable automáticamente con los contratos actuales. Se requiere respaldo/restauración o consulta/replay acordado con Users; no se reconstruyen resultados. |
| G. Mensaje bloqueado en partición | Evento y estado Kafka disponibles | No por el reconciliador de Partidas | Se mantiene la política H04-04: orden por partición, reintentos y offset retenido. Reemitir no desbloquea una partición cuyo consumidor continúa bloqueado. |
| H. Confirmación en DLQ | Evento y error conservados en la DLQ H04-04 | No automáticamente | Requiere inspección y una operación de replay autorizada. No se modificó el comportamiento DLQ ni se reinyectaron mensajes. |
| I. Partida expirada por TTL | El match puede no existir, aunque quede un miembro índiceado | No para recuperar el resultado | Se elimina el miembro obsoleto durante el claim. Si expiró tanto partida como outbox, no hay datos suficientes para reemitir. |
| J. Dos reconciliadores | Índices Redis compartidos y lease por partida | Sí | El claim Lua asigna lease con token; solo el token vigente puede reencolar. Un worker obsoleto queda fenced tras vencimiento o aplicación terminal. |

## D. Diseño

- Se usan dos ZSET: `matches:saga:pending` para observabilidad y `matches:saga:reconcile:due` para consultas acotadas por vencimiento. No se hace un escaneo frecuente de todo Redis.
- El cierre y el evento/outbox actualizan los ZSET en la misma transacción Redis. En el primer arranque se hace un `SCAN match:*` con `COUNT` acotado y se backfillea cada clave mediante `WATCH`/`MULTI`; un marcador evita repetir el barrido en cada ciclo. Claves auxiliares como `match:{id}:progress` no se interpretan como partidas.
- La consulta trabaja por lotes y difiere hasta cumplir la edad mínima. Edad no significa fallo terminal.
- Un script Lua reclama la partida, valida que sea `FINISHED`, `PENDING` y que el evento original corresponda a los identificadores, modo y participantes terminales; registra intentos y crea un lease expirante. Las sagas `CONFIRMED`/`CANCELLED`, faltantes, mal formadas o agotadas no se reemiten.
- La reemisión vuelve a validar estado y lease dentro de Lua. El evento se introduce en el outbox Redis durable, nunca se publica desde una tarea efímera. La búsqueda de duplicados en las primeras 200 entradas de outbox/inflight evita encolados repetidos comunes; aunque una entrega Kafka duplicada ocurra, Users mantiene su deduplicación idempotente.
- Un token nuevo reemplaza al lease vencido; el token viejo ya no puede completar. Si el proceso muere antes de completar, el lease vence y el siguiente ciclo vuelve a intentar. Los intentos tienen backoff exponencial, máximo configurable y un estado `exhausted` para intervención.
- Configuración por entorno: `MATCH_SAGA_RECONCILIATION_ENABLED` (default `true`), `MATCH_SAGA_RECONCILIATION_INTERVAL_SECONDS` (60), `MATCH_SAGA_RECONCILIATION_MIN_AGE_SECONDS` (300), `MATCH_SAGA_RECONCILIATION_BATCH_SIZE` (50), `MATCH_SAGA_RECONCILIATION_LEASE_SECONDS` (30), `MATCH_SAGA_RECONCILIATION_MAX_ATTEMPTS` (5), `MATCH_SAGA_RECONCILIATION_BACKOFF_SECONDS` (30) y `MATCH_SAGA_RECONCILIATION_BACKOFF_MAX_SECONDS` (300). Valores inválidos recurren al default.

## E. Implementación

- `app/repositories/matches.py`: índices, backfill, claim/lease/fencing y reencolado Lua; alta atómica al finalizar y baja atómica al aplicar la saga terminal.
- `app/messaging/saga_reconciler.py`: loop periódico, configuración, backoff y registros acotados.
- `app/main.py`: inicio y cierre controlado del reconciliador junto al runtime.
- `app/observability/http_metrics.py`: contadores por resultado y gauges de cantidad/antigüedad pendiente.
- `tests/test_saga_claim_response.py`: contrato del resultado de Lua/redis-py, respuesta anidada con texto y bytes, respuesta vacía y rechazos explícitos de formas malformadas.
- `tests/test_saga_reconciler.py`, `tests/test_match_saga_repository.py`, `tests/test_main_lifespan.py` y `tests/integration/test_h04_03_redis_lua.py`: cobertura del flujo, ciclo de vida y operaciones reales Redis.
- `tests/integration/test_h04_05_reconciliation_kafka.py`: protocolo de reemisión a Kafka real sobre el runtime/outbox.
- `tests/h04-05/compose.yaml`, `tests/h04-05/Dockerfile` y `tests/run_h04_05_isolated.ps1`: entorno exclusivo Kafka+Redis+pytest, red interna, sin puertos ni volúmenes persistentes y limpieza por proyecto único.
- No se modificaron contratos Kafka, H04-04, reglas XP, otros repositorios, Compose compartido ni opciones de persistencia.

## F. Garantías

- Cierre terminal, evento original y alta de índices Redis suceden en una transacción.
- Aplicación de confirmación/cancelación, ranking, historial y baja de índices se ejecutan en una única Lua; entradas inválidas no aplican cambios.
- La reemisión es at-least-once. La exclusión del reconciliador es por lease/token y la protección de efectos de negocio depende de la deduplicación real en Users y de la transición Lua idempotente en Partidas.
- VERSUS reemite un solo evento con la lista original de ambos participantes; no recalcula ganador, XP o recompensas. CLASSIC y TIMED usan los resultados persistidos de igual forma.
- Un evento que solo está en `inflight` puede coexistir brevemente con una reemisión; el publisher normal recupera inflight al reiniciar. No se garantiza exactly-once Kafka.
- El diseño conserva la garantía de recuperación solo mientras Redis conserve la partida y su evento. No fortalece la durabilidad Redis de despliegue.

## G. Pruebas

Ejecutadas en este entorno:

- `python -m pytest -q tests/test_match_saga_repository.py`: **20 aprobadas**, 1 advertencia de compatibilidad/deprecación de `anyio`/pytest del Python local.
- Tras el fallo de integración reportado, `python -m pytest -q tests/test_saga_claim_response.py tests/test_match_saga_repository.py`: **32 aprobadas**, 1 advertencia de compatibilidad/deprecación de `anyio`/pytest del Python local.
- `python -m compileall -q app tests`: correcto.
- `docker compose --project-name h04-05-static --file tests/h04-05/compose.yaml config --quiet`: correcto.
- `git diff --check`: correcto.

Diagnóstico del primer resultado externo: **184 aprobadas, 5 fallidas, 1 advertencia**. Las cinco fallas terminaban en `IndexError` dentro de `claim_pending_sagas()`. La causa raíz era que `_CLAIM_SAGAS_SCRIPT` retorna dos tablas Lua dentro de una tabla externa: `{claimed, outcomes}`. Redis expone eso a redis-py como dos arreglos anidados: `[claims, outcomes]`. `claims` es una secuencia plana de triples `(match_id, lease_token, event_json)`; `outcomes`, una secuencia de pares `(match_id, resultado)`; sin entradas elegibles, el retorno es `[[], []]`. El parser anterior iteraba con el tamaño de `values[0]` pero leía desde `values[index]` en el nivel superior. El `IndexError` ocurría después de que Lua ejecutaba el claim y dejaba un lease; el lease expira si Python no completa la recuperación.

La corrección decodifica exactamente esas dos listas, acepta `str` o `bytes` UTF-8, valida el nivel superior, múltiplos de 3/2, JSON de evento y estados reconocidos. Una respuesta incoherente lanza `SagaReconciliationResponseError`, se registra explícitamente y no se traduce silenciosamente a “sin sagas”. Se añadió `test_real_redis_claim_script_returns_nested_claim_and_outcome_arrays` para inspeccionar directamente el retorno bruto de Redis en la siguiente ejecución real, además de pruebas unitarias fieles al formato redis-py y sus variantes con bytes.

Evidencia de la repetición externa posterior a la corrección, reportada por el usuario:

- El runner terminó `pytest` con código de salida **0**, la suite reportó **100 %**, se usaron Kafka y Redis reales en contenedores aislados y los contenedores/red temporales se eliminaron. Se reportó una advertencia de deprecación de Starlette. No se proporcionó un conteo exacto de pruebas en el registro compartido, por lo que no se consigna una cifra.
- Los cinco escenarios que habían fallado están presentes en la ejecución integral `pytest -q tests`: `test_real_redis_rebuilds_index_and_reemits_only_after_minimum_age`, `test_real_redis_reconciliation_lease_fences_expired_worker`, `test_real_redis_reemission_is_idempotent_while_original_is_queued`, `test_real_redis_confirmation_wins_race_with_reconciliation` y `test_reconciled_redis_event_is_published_to_real_kafka`.
- La cobertura adicional inspecciona el retorno bruto de Lua con Redis real (`test_real_redis_claim_script_returns_nested_claim_and_outcome_arrays`) y cubre el parser con respuesta anidada, arreglos vacíos, bytes UTF-8, longitudes inválidas, JSON inválido y estados no reconocidos (`tests/test_saga_claim_response.py`).
- El Compose aislado ejecuta todas las pruebas de `tests`, incluidas las regresiones CLASSIC/TIMED/VERSUS, saga, Lua, Kafka/DLQ y HTTP. La cobertura de PostgreSQL/deduplicación real de Users no forma parte de esta ejecución.

## H. Limitaciones

- Pérdida simultánea de la partida y del outbox Redis no puede repararse con los datos disponibles. Propuesta mínima para otra fase: endpoint interno idempotente en Users para consultar/republicar el resultado de una partida por `matchId` o `sourceEventId`, con autenticación de servicio y fingerprint/conflict explícitos; requiere decisión entre servicios.
- La DLQ no se reprocesa automáticamente. Se requiere runbook/operación manual y validación del estado antes de replay.
- Redis puede perder escrituras recientes según la política de persistencia existente; esa política está fuera del alcance H04-05.
- El backfill de primera ejecución recorre claves `match:*` una vez con `SCAN`; en instalaciones grandes se debe planificar el primer arranque y revisar el marcador/tiempo. Los reinicios normales no vuelven a barrer si el marcador persiste.
- Si Redis conserva el marcador pero pierde parcialmente los ZSET, el backfill no se repite automáticamente. La recuperación operativa es borrar solo el marcador H04-05 y reiniciar con control; no se hizo en una base existente.
- El máximo de intentos puede agotar una saga aunque el sistema remoto esté caído más tiempo que el backoff configurado; el miembro pendiente se conserva para diagnóstico y requiere intervención/reinicio controlado de estado.
- La simulación real de deduplicación PostgreSQL de Users no se ejecutó. Para validar extremo a extremo se requiere una composición efímera adicional que construya users-service y PostgreSQL desechable, aislada del stack compartido.

## I. Despliegue

No hay migración SQL. Antes de habilitar en producción, revisar con operaciones: persistencia Redis actual, capacidad/latencia del backfill inicial, edad mínima, máximo de intentos, backoff, umbrales de alerta para `match_saga_reconciliation_pending` y `match_saga_reconciliation_oldest_age_seconds`, y procedimiento para liberar sagas `exhausted`/`unrecoverable`. El interruptor `MATCH_SAGA_RECONCILIATION_ENABLED=false` desactiva la recuperación sin alterar el resto del runtime.

## J. Estado Git

- Rama: `fix/h04-05-saga-reconciliation`.
- Cambios locales únicamente en `partidas-service` y relativos a H04-05, sus pruebas y documentación. Incluyen la corrección del parser Lua/redis-py y las pruebas del contrato de respuesta.
- `git diff --check`: limpio.
- Sin commit, push ni Pull Request.

## K. Recomendación

**APROBAR CON OBSERVACIONES.** El usuario reportó código de salida 0 para la suite integral con Kafka y Redis reales aislados; los cinco escenarios fallidos anteriores están identificados en el conjunto ejecutado. La advertencia de Starlette no fue funcional. Se mantienen las limitaciones de durabilidad Redis, vencimiento TTL, pérdida simultánea de partida/outbox y dependencia de idempotencia de Users; no se probó PostgreSQL de Users dentro de esta composición.
