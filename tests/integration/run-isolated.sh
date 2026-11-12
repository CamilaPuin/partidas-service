#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
service_dir="$(cd "$script_dir/../.." && pwd)"
compose_file="$script_dir/compose.yaml"
project_name="deathdoku-m6-classic-$(uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]')"
users_commit="4b183596229ce5ef5d79c9c6b7e782d6d8022e80"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/m6-classic-integration.XXXXXX")"

export M6_DB_PASSWORD="$(openssl rand -hex 24)"
export M6_MONGO_PASSWORD="$(openssl rand -hex 24)"
export M6_GATEWAY_SECRET="$(openssl rand -hex 32)"
export M6_INTERNAL_TOKEN="$(openssl rand -hex 32)"
export M6_CASES_IMAGE="${M6_CASES_IMAGE:-thedeathdoku-casos-service:latest}"
export M6_EUREKA_IMAGE="${M6_EUREKA_IMAGE:-thedeathdoku-eureka-server:latest}"
export M6_USERS_SOURCE_DIR="$tmp_dir/users-build"

cleanup() {
  if ! docker compose -p "$project_name" -f "$compose_file" down --remove-orphans >/dev/null; then
    echo "Aviso: no se pudo retirar el proyecto Docker aislado $project_name." >&2
  fi
  if [ -d "$tmp_dir" ] && ! rm -rf "$tmp_dir"; then
    echo "Aviso: no se pudo retirar el directorio temporal de integración." >&2
  fi
}

if ! command -v docker >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  echo "Docker y curl son necesarios para la integración aislada." >&2
  exit 1
fi

branch="$(git -C "$service_dir" branch --show-current)"
if [ "$branch" != "feat/m6-classic" ]; then
  echo "Se esperaba partidas-service en feat/m6-classic; rama actual: $branch" >&2
  exit 1
fi

if ! docker image inspect "$M6_CASES_IMAGE" "$M6_EUREKA_IMAGE" >/dev/null 2>&1; then
  echo "Faltan las imágenes estables preexistentes de cases-service o Eureka; no se reconstruirán." >&2
  exit 1
fi

trap cleanup EXIT
mkdir -p "$tmp_dir/users-source" "$M6_USERS_SOURCE_DIR"
curl --fail --location --silent --show-error \
  "https://github.com/DanielEspitiaAgu/users_service/archive/$users_commit.tar.gz" \
  --output "$tmp_dir/users-source.tar.gz"
tar -xzf "$tmp_dir/users-source.tar.gz" -C "$tmp_dir/users-source" --strip-components=1
cp "$tmp_dir/users-source/Dockerfile" \
  "$tmp_dir/users-source/pom.xml" \
  "$tmp_dir/users-source/mvnw" \
  "$tmp_dir/users-source/mvnw.cmd" \
  "$M6_USERS_SOURCE_DIR/"
cp -R "$tmp_dir/users-source/.mvn" "$tmp_dir/users-source/src" "$M6_USERS_SOURCE_DIR/"

if ! docker compose -p "$project_name" -f "$compose_file" up --detach --build --wait --wait-timeout 300; then
  echo "Falló el arranque; registros del broker Kafka aislado:" >&2
  if ! docker compose -p "$project_name" -f "$compose_file" logs --no-color kafka >&2; then
    echo "No se pudieron leer los registros de Kafka." >&2
  fi
  exit 1
fi

wait_for_eureka_app() {
  local app_name="$1"
  for attempt in $(seq 1 60); do
    if docker compose -p "$project_name" -f "$compose_file" exec -T eureka-server \
      wget -q --spider "http://127.0.0.1:8762/eureka/apps/$app_name"; then
      return 0
    fi
    sleep 1
  done
  echo "Eureka no registró $app_name en 60 segundos." >&2
  return 1
}

wait_for_eureka_app CASOS-SERVICE
wait_for_eureka_app USUARIOS-SERVICE

partidas_port="$(docker compose -p "$project_name" -f "$compose_file" port partidas-service 8000 | awk -F: 'NR == 1 {print $NF}')"
cases_port="$(docker compose -p "$project_name" -f "$compose_file" port casos-service 8082 | awk -F: 'NR == 1 {print $NF}')"
users_port="$(docker compose -p "$project_name" -f "$compose_file" port usuarios-service 8081 | awk -F: 'NR == 1 {print $NF}')"

export M6_PARTIDAS_URL="http://127.0.0.1:$partidas_port"
export M6_CASES_URL="http://127.0.0.1:$cases_port"
export M6_USERS_URL="http://127.0.0.1:$users_port"
python3 "$script_dir/verify-services.py"
