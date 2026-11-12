# Integración aislada de M6.1

Esta configuración prueba `partidas-service/feat/m6-classic` contra el contrato
publicado de `users_service/feat/m6-classic-results` y el servicio de casos,
sin utilizar el API Gateway ni recursos del Compose principal.

## Aislamiento

- `run-isolated.sh` crea un nombre de proyecto Docker único y una red privada.
- PostgreSQL, MongoDB, Redis y Kafka usan `tmpfs`, incluidos todos los puntos
  `VOLUME` declarados por las imágenes.
- El Compose no declara ni crea volúmenes y no publica bases de datos.
- Solo los puertos aleatorios de partidas, casos y usuarios se enlazan a
  `127.0.0.1`.
- Partidas se construye desde el checkout actual `feat/m6-classic`; usuarios se
  descarga desde el commit publicado `4b183596229ce5ef5d79c9c6b7e782d6d8022e80`
  y se construye desde un contexto temporal que solo contiene el Dockerfile,
  archivos Maven, `.mvn/` y `src/`.
- Casos y Eureka usan imágenes estables ya existentes. El runner falla si no
  están disponibles y no intenta reconstruirlas.
- Al terminar, el runner baja exclusivamente su proyecto con `docker compose
  down --remove-orphans`; no usa `-v`, elimina volúmenes ni toca otros
  contenedores.
- Las credenciales de prueba se generan en memoria para cada ejecución. No se
  crea ni requiere `.env`.

El Compose efímero habilita Flyway solo para users-service; así aplica V1–V3
antes de aceptar resultados. `JPA_DDL_AUTO=update` se mantiene para completar
el esquema ORM, sin cambiar la configuración de despliegue publicada.

## Requisitos

- macOS con Docker Engine y Docker Compose.
- Java 21 y Maven para probar migraciones.
- Python 3 para el comprobador HTTP, sin paquetes adicionales.
- Las imágenes existentes `thedeathdoku-casos-service:latest` y
  `thedeathdoku-eureka-server:latest`.
- Acceso de red para descargar la rama de usuarios fijada y las imágenes que
  falten para los servicios M6/bases de prueba.

## Comandos

Desde esta carpeta:

```bash
./run-users-migrations.sh
./run-isolated.sh
```

El primer comando ejecuta las pruebas Maven de la rama publicada frente a un
PostgreSQL `16.15` descartable, en tmpfs y con puerto local aleatorio. Incluye
las migraciones V1–V3 y las pruebas transaccionales HTTP/DB de B3.

El segundo comando llama los endpoints reales de partidas, casos y usuarios.
Comprueba:

- dificultad del caso obtenida del servicio de casos;
- intento incorrecto que mantiene `IN_PROGRESS` y no registra recompensa;
- replay idéntico y conflicto por reutilizar `attemptId` con otro contenido;
- victoria CLASSIC, replay idempotente del resultado y conflicto HTTP 409;
- recompensa CLASSIC única por usuario/caso y `uniqueCasesSolved`;
- compatibilidad de registro y XP para TIMED y VERSUS.

El Gateway se excluye deliberadamente de esta prueba; ver
[`GATEWAY_COMPATIBILITY.md`](./GATEWAY_COMPATIBILITY.md).
