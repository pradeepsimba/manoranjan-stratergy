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
# `docker compose down …` and, on failure, escalates through the fixes that
# actually clear it — retrying after each — instead of erroring out.
compose_down() {
    local args=("$@")
    docker compose "${args[@]}" && return 0

    echo ""
    echo "⚠️  'docker compose down' failed — likely the Ubuntu AppArmor issue."
    echo "    Attempting automatic recovery (needs sudo)…"

    # 1) Reload AppArmor — this rebuilds the docker-default profile and is the
    #    single most common fix (more so than restarting Docker).
    echo "    → sudo systemctl restart apparmor"
    sudo systemctl restart apparmor >/dev/null 2>&1 || true
    sleep 1
    docker compose "${args[@]}" && { echo "✅ Recovered after reloading apparmor."; return 0; }

    # 2) Remove stale/unknown AppArmor profiles (needs apparmor-utils).
    if command -v aa-remove-unknown >/dev/null 2>&1; then
        echo "    → sudo aa-remove-unknown"
        sudo aa-remove-unknown >/dev/null 2>&1 || true
        docker compose "${args[@]}" && { echo "✅ Recovered after aa-remove-unknown."; return 0; }
    else
        echo "    (skipping aa-remove-unknown — install 'apparmor-utils' to enable it)"
    fi

    # 3) Restart the Docker daemon (reloads its AppArmor integration).
    echo "    → sudo systemctl restart docker"
    if sudo systemctl restart docker >/dev/null 2>&1; then
        for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 1; done
        docker compose "${args[@]}" && { echo "✅ Recovered after restarting Docker."; return 0; }
    fi

    # 4) Last resort: the container is still unstoppable via Docker (e.g.
    #    live-restore keeps it across daemon restarts). Kill its process
    #    directly at the OS level, force-remove it, then retry for the rest
    #    (volumes / images).
    echo "    → force-killing stuck container process(es)"
    local ids id pid
    ids=$(docker compose ps -aq 2>/dev/null)
    for id in $ids; do
        pid=$(docker inspect -f '{{.State.Pid}}' "$id" 2>/dev/null)
        if [ -n "$pid" ] && [ "$pid" != "0" ]; then
            sudo kill -9 "$pid" 2>/dev/null || true
        fi
        docker rm -f "$id" >/dev/null 2>&1 || sudo docker rm -f "$id" >/dev/null 2>&1 || true
    done
    docker compose "${args[@]}" && { echo "✅ Recovered after force-killing containers."; return 0; }

    # 5) Give up with the manual escalation steps.
    echo ""
    echo "❌ Automatic recovery failed. Try manually, then re-run:"
    echo "    sudo apt-get install -y apparmor-utils   # provides aa-remove-unknown"
    echo "    sudo aa-remove-unknown"
    echo "    sudo systemctl restart apparmor && sudo systemctl restart docker"
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
