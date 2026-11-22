# Runner aislado H04-03

Desde la raíz de `partidas-service`, ejecutar en PowerShell:

```powershell
.\tests\run_h04_03_isolated.ps1
```

El runner exige acceso a Docker Engine, valida el Compose antes de iniciarlo y
genera un nombre de proyecto único. Compose crea solo Redis y un contenedor
Linux de pytest, conectados por una red interna propia. Redis no publica
puertos, desactiva RDB/AOF y monta `/data` como tmpfs efímero (sin volumen
persistente). El contenedor Linux ejecuta
`pytest -q tests --ignore=tests/integration/test_h04_04_kafka_dlq.py`, incluidos
los casos H04-03 que llaman el script Lua real y las regresiones HTTP de
CLASSIC, TIMED y VERSUS. La suite Kafka se ejecuta exclusivamente con el runner
aislado H04-04, que sí levanta un broker.

El bloque `finally` ejecuta `docker compose down --remove-orphans` únicamente
con el archivo y el nombre de proyecto de esta ejecución y comprueba que no
queden contenedores ni redes con esa etiqueta. No ejecuta `down -v`.

Las pruebas Redis usan `H04_TEST_REDIS_HOST` y `H04_TEST_REDIS_PORT`. Fuera de
este runner, los casos reales se omiten si no se define el host; las pruebas
unitarias con Redis simulado siguen disponibles por separado.
