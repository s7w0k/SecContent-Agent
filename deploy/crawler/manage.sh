#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_NAME=${MCP_CRAWL_PROJECT_NAME:-pr-crawler}
ENV_FILE=${MCP_CRAWL_ENV_FILE:-$SCRIPT_DIR/.env.crawler}
COMPOSE_FILE=$SCRIPT_DIR/docker-compose.yml
BUILD_FILE=$SCRIPT_DIR/docker-compose.build.yml
ACTION=${1:-status}
TAG=${2:-}

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; copy .env.crawler.example and set MCP_CRAWL_API_KEY." >&2
  exit 2
fi

compose() {
  docker compose -p "$PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

case "$ACTION" in
  build)
    docker compose -p "$PROJECT_NAME" --env-file "$ENV_FILE" \
      -f "$COMPOSE_FILE" -f "$BUILD_FILE" build
    ;;
  up)
    compose up -d --no-build --remove-orphans
    ;;
  upgrade)
    compose pull
    compose up -d --no-build --remove-orphans
    ;;
  rollback)
    if [ -z "$TAG" ]; then
      echo "Usage: $0 rollback <image-tag>" >&2
      exit 2
    fi
    MCP_CRAWL_IMAGE_TAG=$TAG compose pull
    MCP_CRAWL_IMAGE_TAG=$TAG compose up -d --no-build --remove-orphans
    ;;
  down)
    compose down
    ;;
  logs)
    compose logs -f --tail 200
    ;;
  status)
    compose ps
    ;;
  config)
    compose config
    ;;
  *)
    echo "Usage: $0 {build|up|upgrade|rollback <tag>|down|logs|status|config}" >&2
    exit 2
    ;;
esac
