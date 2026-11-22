# H04-03 — Validación de outcomes de saga

`apply_saga_outcome()` compara las confirmaciones `CONFIRMED` con la partida y
el evento terminal almacenados en Redis. El evento Kafka conserva el contrato
actual; no se agregan campos.

La validación comprueba el `matchId`, modo, `caseId`, correlación e identidad
del evento; exige que la partida esté `FINISHED`; y compara el conjunto exacto
de usuarios con `player1`/`player2` y los resultados terminales persistidos.
Cada participante aparece una sola vez, el booleano `won` coincide, y
`xpDelta` debe ser un entero finito. El `xpDelta` recibido no se iguala al del
evento terminal porque Usuarios puede calcular o transformar recompensas; la
aplicación mantiene el valor confirmado por Usuarios. Para VERSUS también se
comprueba que el ganador terminal sea exactamente uno de los dos jugadores y
coincida con `winnerId`.

El cambio de estado, ranking e historial se ejecuta mediante un único script
Lua. Antes de escribir, el script compara la versión leída de la partida,
comprueba los tipos de las claves destino y verifica que las puntuaciones
resultantes sean finitas. Así se evitan escrituras parciales causadas por
outcomes inválidos, carreras o tipos de clave incompatibles. Una confirmación
repetida con el mismo contenido no vuelve a aplicar recompensas; una distinta
después de confirmar devuelve `conflict`.

## Manejo de offsets Kafka (H04-04)

La política vigente para `invalid`, `stale`, `conflict`, Redis y offsets está
documentada en [`h04-04-kafka-outcome-consumer.md`](h04-04-kafka-outcome-consumer.md).
Los errores permanentes conservan el payload original en DLQ antes del commit;
los fallos transitorios mantienen el offset sin confirmar y la idempotencia Lua
de esta sección protege los replays.

`tests/run_h04_03_isolated.ps1` prepara la validación con Redis real y las
regresiones HTTP en Linux. Desde la raíz del repositorio, se ejecuta con:

```powershell
.\tests\run_h04_03_isolated.ps1
```

La ejecución real queda pendiente desde una sesión de PowerShell con acceso a
Docker Engine. La sesión de Codex no tiene permiso para conectar con el motor;
las pruebas locales con fake no sustituyen esa validación.
