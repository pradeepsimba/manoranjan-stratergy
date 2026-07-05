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

# ── AppArmor-resilient `docker compose down` ──────────────────────────────────
# On Ubuntu the container's docker-default AppArmor profile can go "unknown"
# (after an apparmor reload or a Docker/kernel update); the kernel then blocks
# the stop/kill syscall with "permission denied". This runs the given
# `docker compose down …` and, on that failure, auto-recovers and retries
# instead of erroring out.
compose_down() {
    if docker compose "$@"; then
        return 0
    fi

    echo ""
    echo "⚠️  'docker compose down' failed — likely the Ubuntu AppArmor issue."
    echo "    Attempting automatic recovery (needs sudo)…"

    # 1) Clear stale/unknown AppArmor profiles, then retry.
    if command -v aa-remove-unknown >/dev/null 2>&1; then
        echo "    → sudo aa-remove-unknown"
        sudo aa-remove-unknown >/dev/null 2>&1 || true
        if docker compose "$@"; then
            echo "✅ Recovered after aa-remove-unknown."
            return 0
        fi
    fi

    # 2) Restart the Docker daemon (reloads the docker-default profile and
    #    stops lingering containers), then retry.
    echo "    → sudo systemctl restart docker"
    if sudo systemctl restart docker >/dev/null 2>&1; then
        # Wait for the daemon socket to come back before retrying.
        for _ in $(seq 1 15); do
            docker info >/dev/null 2>&1 && break
            sleep 1
        done
        if docker compose "$@"; then
            echo "✅ Recovered after restarting Docker."
            return 0
        fi
    fi

    # 3) Give up with the manual escalation steps.
    echo ""
    echo "❌ Automatic recovery failed. Run these manually, then retry:"
    echo "    sudo aa-remove-unknown"
    echo "    sudo systemctl restart apparmor"
    echo "    sudo systemctl restart docker"
    return 1
}

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
        compose_down down || exit 1
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
        compose_down down --volumes --rmi all --remove-orphans || exit 1
        ;;
    *)
        echo "Unknown action: $ACTION"
        echo "Usage: bash docker.sh [build | deploy | stop | logs | restart | remove]"
        exit 1
        ;;
esac
