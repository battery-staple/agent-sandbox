#!/usr/bin/env bash
set -e

if [ "$#" -eq 0 ]; then
    echo "=========================================================="
    echo "  Starting Antigravity Sandboxed Runtime Container"
    echo "  Web UI Port     : 58432 (127.0.0.1 loopback bridged)"
    echo "  Workspace Mount : /workspace"
    echo "=========================================================="

    echo "[Antigravity] Launching Antigravity Language Server Daemon..."
    echo "[Antigravity] Bridging port 58432 (0.0.0.0) -> language_server (127.0.0.1:58431)..."
    socat TCP-LISTEN:58432,fork,bind=0.0.0.0,reuseaddr TCP:127.0.0.1:58431 2>/dev/null &

    echo "[Antigravity] Executing Language Server: /usr/local/bin/language_server"
    exec /usr/local/bin/language_server \
        --standalone \
        --override_ide_name antigravity \
        --subclient_type hub \
        --override_ide_version 2.8.1 \
        --override_user_agent_name antigravity \
        --https_server_port 58431 \
        --csrf_token "${CSRF_TOKEN:-antigravity-secure-token}" \
        --app_data_dir antigravity \
        --api_server_url https://generativelanguage.googleapis.com \
        --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com
else
    exec "$@"
fi
