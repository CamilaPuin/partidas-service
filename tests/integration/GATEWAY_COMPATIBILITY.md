# Diferencias observadas en API Gateway

Auditoría de solo lectura. No se modificó ni se desplegó el Gateway.

## Ramas y configuración comparadas

- El checkout local de `apigateway` estaba en `main`, commit `c6d5a34`, con
  `origin/main` y `origin/Semana-1-y-2` disponibles remotamente.
- `main` valida tokens Keycloak y propaga `X-User-Id`, `X-Roles` y
  `X-Gateway-Secret`.
- `Semana-1-y-2` contiene la implementación anterior con JWT HMAC; no es la
  misma configuración de identidad que `main`.
- En ambas configuraciones revisadas, la ruta de casos es `/casos/**` con
  `StripPrefix=1`, que coincide con el controlador `POST /cases/:id/validate`
  de cases-service.
- En ambas, el bloqueo explícito del Gateway apunta a
  `/usuarios/users/*/results/**`. No se encontró una regla equivalente para
  `/casos/cases/*/validate`.

## Implicación

La lectura del código no explica por sí sola el 403 que Bruno observa al probar
`POST /casos/cases/{id}/validate` a través del entorno estable. La prueba puede
estar alcanzando otra versión/configuración, o el rechazo puede producirse en
otra capa. No se cambia ni se infiere una regla del Gateway a partir de ese
resultado: el equipo responsable verificará la protección en el entorno
estable.

La integración M6 aislada invoca partidas-service, que a su vez consulta
cases-service con `X-Gateway-Secret`; no prueba ni altera la ruta pública del
Gateway. Su resultado no sustituye la regresión Bruno de acceso público.
