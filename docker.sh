#!/bin/bash

# docker.sh - Manage building, deploying, and monitoring Docker Compose services.

ACTION=$1

if [ -z "$ACTION" ]; then
    echo "Usage: bash docker.sh [build | deploy | stop | logs | restart]"
    echo "  --build, build     Build the Docker images"
    echo "  --deploy, deploy   Start the services in the background"
    echo "  --stop, stop       Stop and remove the services"
    echo "  --logs, logs       Watch application logs"
    echo "  --restart, restart Restart the services"
    exit 1
fi

case "$ACTION" in
    build|--build)
        echo "=== Building Docker services ==="
        docker compose build
        ;;
    deploy|--deploy|start|--start)
        echo "=== Deploying Docker services in background ==="
        docker compose up -d
        ;;
    stop|--stop|down|--down)
        echo "=== Stopping Docker services ==="
        docker compose down
        ;;
    logs|--logs)
        echo "=== Streaming app logs (Ctrl+C to exit) ==="
        docker compose logs -f app
        ;;
    restart|--restart)
        echo "=== Restarting Docker services ==="
        docker compose restart
        ;;
    *)
        echo "Unknown action: $ACTION"
        echo "Usage: bash docker.sh [build | deploy | stop | logs | restart]"
        exit 1
        ;;
esac
