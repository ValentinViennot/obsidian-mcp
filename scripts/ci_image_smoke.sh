#!/usr/bin/env bash
# Exercise the candidate's real entrypoint, migrations and lifespan on synthetic data.
set -euo pipefail
image=${1:?candidate image required}
root=$(cd "$(dirname "$0")/.." && pwd)
name="omcp-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$"
fixture=$(mktemp -d)
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then docker logs "$name-app" 2>&1 || true; fi
  docker rm -f "$name-app" "$name-provider" "$name-db" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  rm -rf "$fixture"
}
trap cleanup EXIT
mkdir "$fixture/vault"
printf '# Synthetic release fixture\n\nA disposable release validation note.\n' > "$fixture/vault/fixture.md"
chmod 755 "$fixture"
chmod 777 "$fixture/vault"
cat > "$fixture/provider.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        inputs = payload.get('input', [])
        if isinstance(inputs, str):
            inputs = [inputs]
        body = json.dumps({'embeddings': [[1.0] + [0.0] * 63 for _ in inputs]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(('0.0.0.0', 11434), Handler).serve_forever()
PY
docker network create --internal "$name" >/dev/null
docker run -d --name "$name-db" --network "$name" --network-alias database \
  -e POSTGRES_PASSWORD=synthetic-ci-only pgvector/pgvector:pg16 >/dev/null
ready=false
for _ in $(seq 1 60); do
  if docker exec "$name-db" pg_isready -U postgres -q; then ready=true; break; fi
  sleep 1
done
[ "$ready" = true ]
docker exec -i "$name-db" psql -U postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE smoke LOGIN NOSUPERUSER PASSWORD 'synthetic-ci-only';
CREATE DATABASE smoke OWNER smoke;
\connect smoke
CREATE EXTENSION vector;
SQL
docker run -d --name "$name-provider" --network "$name" --network-alias provider \
  --entrypoint python -v "$fixture/provider.py:/provider.py:ro" "$image" /provider.py >/dev/null
docker run -d --name "$name-app" --network "$name" --memory 2g --cpus 2 --pids-limit 256 \
  -e DATABASE_URL=postgresql+asyncpg://smoke:synthetic-ci-only@database:5432/smoke \
  -e DATABASE_SSL_MODE=disable -e SECRET_KEY=synthetic-release-smoke-secret-not-production \
  -e BASE_URL=http://localhost:8000 -e VAULT_PATH=/obsidian \
  -e OLLAMA_URL=http://provider:11434 -e EMBEDDING_ALLOW_PLAINTEXT=true \
  -e EMBEDDING_DIMENSIONS=64 -e RUN_MIGRATIONS=true \
  -v "$fixture/vault:/obsidian" "$image" >/dev/null
ready=false
for _ in $(seq 1 90); do
  if docker exec "$name-app" python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://localhost:8000/health")); assert d["status"]=="ok"' >/dev/null 2>&1; then
    # Require actual indexing and vectors; an early healthy lifespan alone is insufficient.
    count=$(docker exec "$name-db" psql -U postgres -d smoke -Atc 'SELECT count(*) FROM note_embeddings WHERE valid_to IS NULL' 2>/dev/null || true)
    if [[ "$count" =~ ^[1-9][0-9]*$ ]]; then ready=true; break; fi
  fi
  sleep 1
done
[ "$ready" = true ]
[ "$(docker exec "$name-app" id -u)" = 1000 ]
[ "$(docker exec "$name-app" curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/mcp/)" = 401 ]
docker exec "$name-app" alembic check
expected=$(python3 "$root/scripts/ci_release.py" schema-head)
actual=$(docker exec "$name-db" psql -U postgres -d smoke -Atc 'SELECT version_num FROM alembic_version')
[ "$actual" = "$expected" ]
printf 'Candidate startup, synthetic indexing, non-root/auth guards and schema %s passed.\n' "$actual"
