# H04-04 — Informe técnico de implementación

## A. Estado inicial

- Repositorio: `partidas-service`.
- La rama local `main` y `origin/main` estaban en `0dca48e`, pero el PR #14
  ya estaba fusionado remotamente en `c22da9404537e8d3feeb9096f22585b2da85c38c`.
- Tras autorización expresa, se ejecutó `git fetch origin main`, se verificó
  que `origin/main` contenía #14 y que el árbol estaba limpio, se avanzó `main`
  con `git merge --ff-only origin/main` y se creó
  `fix/h04-04-kafka-outcome-dlq` desde `c22da94`.
- No se crearon commits, pushes ni Pull Requests.

## B. Diagnóstico

`MatchSagaRuntime` procesaba todos los outcomes en el ciclo principal. Una
excepción Redis terminaba el ciclo y reiniciaba todo el consumidor; el offset
permanecía sin commit, pero se interrumpía el trabajo de todas las particiones.
JSON malformado tampoco tenía una ruta DLQ explícita. Los resultados
`invalid`, `stale` y `conflict` se registraban como warning y se confirmaban
indistintamente, aunque `invalid` y `conflict` pueden representar datos
permanentemente inconsistentes.

H04-03 devuelve `stale` cuando el `correlationId` ya no coincide con la saga
actual; la aplicación Lua valida el resto del snapshot terminal y hace
idempotentes las transiciones y recompensas.

## C. Política de errores

| Categoría | Tratamiento | Offset |
|---|---|---|
| Outcome aplicado o duplicado | Aplicación Lua / no repetir XP ni historial | Commit al concluir |
| `stale` por correlación antigua | Descartar con trazabilidad | Commit |
| JSON/esquema inválido | DLQ con payload/key originales y metadatos | Commit tras ACK DLQ |
| `invalid`, `conflict`, `not_found` | DLQ como inconsistencia permanente | Commit tras ACK DLQ |
| Redis, DLQ o commit temporalmente fallido | Reintentos acotados con backoff; tras agotar un ciclo se pausa solo esa partición y se inicia otro ciclo | No confirmar |
| Error inesperado | Stack trace, mantener la partición detenida y reintentar con espera | No confirmar |

No se convierte un error técnico en `CANCELLED`. La definición completa de
headers y criterios está en `h04-04-kafka-outcome-consumer.md`.

## D. Implementación

- `app/messaging/match_saga_runtime.py`: valida la forma del evento, envía
  inválidos y conflictos a `matches.saga.confirmed.v1.dlq`, confirma offsets
  solo después del tratamiento, incorpora backoff configurable, workers FIFO
  separados por partición y cancelación de workers en rebalances. La recuperación
  de outbox H04-01 ahora se reintenta dentro de la tarea de fondo para que una
  indisponibilidad Redis al inicio no impida arrancar el lifespan.
- `tests/test_match_saga_runtime.py`: 20 casos recopilables para orden DLQ/commit,
  validación, negocio, reintentos, fallos de broker, recuperación, rebalances,
  orden y aislamiento entre particiones.
- `tests/integration/test_h04_04_kafka_dlq.py`: integración desechable con Kafka
  y Redis reales para payload DLQ, orden/offset, duplicados y redelivery tras
  reinicio después de aplicar Redis.
- `tests/h04-04/compose.yaml`, `tests/h04-04/Dockerfile` y
  `tests/run_h04_04_isolated.ps1`: runner temporal con red interna, nombre de
  proyecto único, sin puertos expuestos ni volúmenes persistentes, y limpieza
  limitada a los recursos del proyecto.
- `docs/h04-04-kafka-outcome-consumer.md`: política y ejecución.
- `docs/h04-03-saga-outcome-validation.md`: actualiza la nota que antes dejaba
  el manejo de offsets pendiente de H04-04.

## E. Garantías y límites

- Kafka tiene `enable_auto_commit=False`; cada commit usa exclusivamente el
  siguiente offset del mensaje concluido.
- Si falla el commit después de completar Redis/DLQ, el mismo proceso reintenta
  solo el commit. Si hay reinicio y Kafka redelivery, Redis devuelve `duplicate`
  sin volver a aplicar ranking, historial o transición.
- Cada partición tiene un worker FIFO. El worker pausa la partición mientras
  espera y permite que otras particiones sigan procesando.
- Un replay después de aplicar Redis pero antes de confirmar Kafka vuelve a
  pasar por la idempotencia de H04-03; no repite XP ni historial.
- La DLQ conserva bytes originales y key, además de topic/partición/offset,
  tipo y descripción del error, hora, correlación cuando existe e intento de
  clasificación. Si el ACK DLQ ocurre y el commit original falla, puede haber
  un registro DLQ duplicado; consumidores de DLQ deben deduplicar por origen.
- Un error Redis persistente mantiene la partición sin commit y reintenta con
  espera acotada indefinidamente. Esto preserva recuperación y orden; requiere
  monitorización operativa.
- La política no crea la DLQ en infraestructura compartida. El topic debe
  existir y tener retención/ACL apropiadas en el entorno de despliegue. Si el
  broker no permite publicar, el offset no avanza.

## F. Pruebas automatizadas

**Comprobaciones ejecutadas:**

- `python -m compileall -q app tests`: aprobado.
- `python -m pytest --collect-only -q tests/test_match_saga_runtime.py`: 20
  pruebas recopiladas.
- `python -m pytest --collect-only -q tests/integration/test_h04_04_kafka_dlq.py`:
  1 prueba recopilada.
- `docker compose --project-name h04-04-static-validation --file
  tests/h04-04/compose.yaml config --quiet`: aprobado.
- Parseo PowerShell y comprobación ASCII de `run_h04_04_isolated.ps1`: aprobados.
- `git diff --check`: aprobado.
- Import de `MatchSagaRuntime` y `ConsumerRebalanceListener`: aprobado.

**Ejecución pendiente:** el intento de pytest se detuvo en la primera prueba
asíncrona tras 30 segundos. También se observó que el entorno local Windows
(.venv Python 3.13.1) no terminaba ni un `asyncio.run(asyncio.sleep(0))`. No se
declara aprobada ninguna de las 21 pruebas recopiladas.

## G. Integración real

El runner está listo; el comando desde PowerShell, en la raíz de
`partidas-service`, es:

```powershell
.\tests\run_h04_04_isolated.ps1
```

Incluye las regresiones existentes de CLASSIC, TIMED, VERSUS, HTTP, saga y Lua
Redis, junto con las pruebas H04-04 y una integración Kafka/Redis real. En este
entorno `docker info` terminó con código `1`; no se inició ningún contenedor.
Por tanto, Kafka real, Redis real, DLQ efectiva, recuperación tras reinicio y
las regresiones completas permanecen pendientes de ejecución en el PowerShell
del usuario.

La prueba de integración automatizada cubre publicación efectiva en DLQ,
confirmación posterior, evento inválido seguido de válido en la misma
partición, duplicación y replay después de reiniciar el consumidor. Fallo
físico del broker, caída real de Redis y rebalance entre dos procesos no se
simulan como si fueran fallos de infraestructura; requieren validación externa.

## H. Compatibilidad

No se modificaron contratos Kafka de `matches.finished.v1` ni
`matches.saga.confirmed.v1`, reglas XP, transiciones de negocio ni el servicio de
Usuarios. El publicador outbox permanece en el runtime y conserva la recuperación
de mensajes en vuelo H04-01. Las pruebas de regresión existentes se ejecutarán
con el runner, pero no se pudieron ejecutar en esta sesión.

## I. Riesgos pendientes

- Ejecutar el runner real y corregir cualquier fallo que revele el entorno Linux.
- Validar rebalance con dos consumidores/procesos y caída/reinicio real de Kafka.
- Validar una indisponibilidad real de Redis mientras se procesa un mensaje.
- Provisionar DLQ, retención y permisos Kafka antes del despliegue.
- Vigilar la pausa indefinida de una partición ante errores persistentes y
  duplicados DLQ cuando falle el commit posterior al ACK.

## J. Estado Git

Los cambios se limitan a consumidor, documentación y pruebas H04-04 (más la
actualización del documento H04-03 para retirar la nota pendiente). La rama es
`fix/h04-04-kafka-outcome-dlq`, basada en `c22da94`. `git diff --check` pasó.
No se hicieron commits, pushes ni Pull Requests.

## K. Recomendación

**PENDIENTE DE VALIDACIÓN.** La política, implementación y runner aislado están
preparados, pero no recomiendo aprobar hasta ejecutar las pruebas unitarias y
la integración real con el runner PowerShell. La evidencia estática no
sustituye esas ejecuciones.

## Seguimiento de los dos fallos del runner

El resultado previo comunicado para la integración real fue `175 passed, 2
failed`. La causa de ambos se identificó y se añadió regresión:

1. **Offset observado en 2, esperado 3:** la prueba esperaba a que cambiara el
   estado Redis de la primera confirmación, pero no a que se procesara el
   siguiente mensaje duplicado de la misma partición. El commit `2` era válido
   para el primer mensaje aplicado mientras el duplicado seguía pendiente. Se
   añadió espera observable al offset esperado completo (`duplicate.offset + 1`)
   con timeout y diagnóstico del último valor, sin reducir el criterio. El
   consumidor ahora registra fallos de commit, asignación actual y ACK de
   commit para diferenciar carrera, pérdida de asignación y error de broker.
2. **Dos efectos en el fake tras commit fallido:** el fake devolvía siempre
   `confirmed` e incrementaba un contador en cada llamada, por lo que no
   representaba la semántica de Redis (`confirmed` una vez y `duplicate` en
   redelivery). También se evitaba una llamada innecesaria dentro del mismo
   proceso: una vez aplicado Redis/DLQ, el runtime reintenta solo el commit. La
   prueba ahora modela idempotencia y conserva la aserción de un único efecto;
   la integración real registra que el replay tras reinicio devuelve
   `duplicate` y comprueba ranking e historial sin incrementos.

La corrección del test de offset es de sincronización/observabilidad; no se
encontró un fallo productivo de commit. La operación repetida del consumidor
era segura frente al Redis real, aunque innecesaria dentro del mismo proceso;
queda optimizada. La nueva ejecución del runner no se pudo realizar desde este
entorno porque `docker info` no pudo conectarse al Engine y pytest asíncrono se
atascó en Windows, por lo que ambos fallos todavía deben confirmarse corregidos
con el runner PowerShell antes de aprobar.
