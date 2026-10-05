#!/bin/sh
set -eu

# Tail logs for the staging container.
sudo podman logs --tail 100 -f crypto-agent-staging
