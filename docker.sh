#!/bin/bash

# docker.sh - Manage building, deploying, and monitoring Docker Compose services.

ACTION=$1

if [ -z "$ACTION" ]; then
    echo "Usage: bash docker.sh [build | deploy | stop | logs | restart | remove]"
    echo "  --build, build     Build the Docker images"
    echo "  --deploy, deploy   Start the services in the background"
    echo "  --stop, stop       Stop and remove the services"
    echo "  --logs, logs       Watch application logs"
    echo "  --restart, restart Restart the services"
    echo "  --remove, remove   Stop services and remove containers, volumes, and images"
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
        if ! docker compose down; then
            echo ""
            echo "⚠️  Error: Docker failed to stop the containers (Permission Denied)."
            echo "This is commonly caused by AppArmor profiles blocking the Docker daemon on Ubuntu."
            echo "Please try running the following command to resolve this:"
            echo "  sudo aa-remove-unknown"
            echo "Or restart the Docker service:"
            echo "  sudo systemctl restart docker"
            exit 1
        fi
        ;;
    logs|--logs)
        echo "=== Streaming app logs (Ctrl+C to exit) ==="
        docker compose logs -f app
        ;;
    restart|--restart)
        echo "=== Restarting Docker services ==="
        docker compose restart
        ;;
    remove|--remove|clean|--clean)
        echo "=== Removing Docker services, volumes, and images ==="
        if ! docker compose down --volumes --rmi all --remove-orphans; then
            echo ""
            echo "⚠️  Error: Docker failed to remove the containers (Permission Denied)."
            echo "This is commonly caused by AppArmor profiles blocking the Docker daemon on Ubuntu."
            echo "Please try running the following command to resolve this:"
            echo "  sudo aa-remove-unknown"
            echo "Or restart the Docker service:"
            echo "  sudo systemctl restart docker"
            exit 1
        fi
        ;;
    *)
        echo "Unknown action: $ACTION"
        echo "Usage: bash docker.sh [build | deploy | stop | logs | restart | remove]"
        exit 1
        ;;
esac
