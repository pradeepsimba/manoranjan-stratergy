# Algo trading 


        docker run --detach \
            --rm \
            --hostname="$(hostname)" \
            --publish="1536:3389/tcp" \
            --name="remote-desktop" \
            scottyhardy/docker-remote-desktop:latest