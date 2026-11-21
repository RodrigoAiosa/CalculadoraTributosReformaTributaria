#!/bin/sh
set -eu

case "${ENGINE:-regular}" in
  regular) DEFAULT_PORT=8080 ;;
  simples) DEFAULT_PORT=8082 ;;
  *) echo "ENGINE invalido: '${ENGINE}' (use 'regular' ou 'simples')" >&2; exit 1 ;;
esac

PORT="${PORT:-$DEFAULT_PORT}"

# O motor precisa rodar a partir do seu diretório para achar ./calculadora/db.
cd "/app/${ENGINE:-regular}"

# shellcheck disable=SC2086
exec java ${JAVA_OPTS:-} \
  -Dspring.profiles.active=offline \
  -jar app.jar \
  --server.port="${PORT}"
