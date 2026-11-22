# H04-04 — Errores y recuperación del consumidor de outcomes

## Política de mensajes y offsets

El consumidor procesa cada partición en orden. Mantiene una cola y un worker
por `TopicPartition`; la partición se pausa mientras tiene trabajo pendiente y
se reanuda al quedar al día. Cada worker confirma `offset + 1` solo después de
terminar el efecto o la transferencia confirmada a DLQ. No hay auto-commit.

| Resultado | Tratamiento | Offset |
|---|---|---|
| `confirmed` / `cancelled` | Aplica la transición Lua de H04-03 | Confirmar después del resultado |
| `duplicate` | Idempotente; no repite XP ni historial | Confirmar |
| `stale` | Descartar con warning; Redis solo lo produce ante mismatch de correlación con la saga actual | Confirmar |
| `invalid` | Inconsistencia estructural/terminal permanente; DLQ con bytes originales y metadatos | Confirmar solo después del ACK DLQ |
| `conflict` | Contradicción con una transición ya aplicada; DLQ para diagnóstico | Confirmar solo después del ACK DLQ |
| `not_found` | La partida no está disponible para validar/aplicar; DLQ para inspección y reproceso controlado | Confirmar solo después del ACK DLQ |
| Redis/Kafka temporalmente indisponible | Reintentos limitados por ciclo; luego se mantiene la partición pausada y se reintenta con backoff acotado | No confirmar mientras persista |
| Excepción inesperada/programación | Stack trace, mantener partición detenida y volver a intentar con espera; nunca convertir en invalid ni confirmar | No confirmar |

`stale` queda reservado a correlaciones antiguas. Los `conflict` no se descartan
como si fueran stale. `invalid`, `conflict` y `not_found` se preservan en
`matches.saga.confirmed.v1.dlq`; no se reprocesan automáticamente.

## Contrato DLQ

El value y key son byte por byte los del mensaje original. Kafka headers
agregan `original-topic`, `original-partition`, `original-offset`, `error-type`,
`error-description`, `failed-at`, `attempts` y `correlation-id` cuando se pudo
extraer. Los mensajes originales malformados no se escriben en logs. `attempts`
cuenta el intento de clasificación que produjo el envío; no intenta inferir
redeliveries previas que Kafka no expone con fiabilidad.

El productor espera `send_and_wait` (producer configurado con `acks=all`). Si
falla publicación, commit o Redis, el offset de entrada queda sin confirmar. Un
fallo después de que Redis aplicó puede causar redelivery; la protección Lua de
H04-03 convierte ese replay en `duplicate` sin repetir recompensas.

Si falla el commit dentro del proceso actual, el runtime reintenta solamente el
commit del mismo offset; no vuelve a invocar la operación de negocio ni a
publicar de nuevo en DLQ. Si el proceso se reinicia antes de confirmar, el
mensaje se procesa de nuevo y la idempotencia Redis es la garantía de seguridad.

## Reintentos y rebalances

Variables configurables:

- `KAFKA_SAGA_RETRY_MAX_ATTEMPTS` (default `3`, mínimo `1`).
- `KAFKA_SAGA_RETRY_BACKOFF_SECONDS` (default `0.5`).
- `KAFKA_SAGA_RETRY_BACKOFF_MAX_SECONDS` (default `10`).

Al agotarse un ciclo, la partición permanece pausada y se inicia otro ciclo
después de una espera acotada; otras particiones asignadas siguen procesando.
No se abandona un outcome confirmado sin DLQ ni se transforma un error técnico
en `CANCELLED`. En `on_partitions_revoked` se cancelan los workers y se descarta
su cola en memoria sin commit; cualquier aplicación Redis que haya terminado
concurrentemente se resuelve al redelivery mediante idempotencia. Un worker por
partición procesa FIFO y confirma cada offset antes de avanzar al siguiente.

## Validación

Pruebas unitarias: `tests/test_match_saga_runtime.py`. Incluyen orden DLQ/commit,
payload original, resultados de negocio, Redis caído/recuperado, fallo DLQ,
commit fallido, error de programación y aislamiento entre particiones.

Integración Kafka + Redis desechables:

```powershell
.\tests\run_h04_04_isolated.ps1
```

El runner comprueba Docker Compose antes de arrancar, asigna nombre de proyecto
aleatorio, usa una red interna propia, no publica puertos ni monta volúmenes
persistentes, y al final elimina solo los recursos de ese proyecto (sin `-v`).
Ejecuta todas las pruebas bajo `tests/`, incluidas regresiones HTTP, saga y Lua
Redis de H04-03, además de una prueba real Kafka/Redis para DLQ, offsets,
duplicados y redelivery tras reinicio.

## Observaciones pendientes

La garantía de orden no depende de commits concurrentes: cada partición tiene un
worker FIFO. La cobertura de integración automatiza reinicio/replay y ACK de
DLQ, pero una caída física del broker, rebalance coordinado con dos procesos y
una interrupción de red real de Redis requieren la ejecución del runner y una
interrupción controlada del contenedor; no se simulan como si fueran pruebas de
infraestructura real. El runner de esta fase no hace reprocesamiento de DLQ.
