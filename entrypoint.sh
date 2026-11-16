#!/bin/sh

# No privilege drop. Run OpenList as the current user (root under orkestr's constraints).
# orkestr drops CAP_CHOWN, CAP_SETGID, CAP_SETUID, so chown and su-exec cannot work.
# 不再降权。以当前用户（orkestr 约束下为 root）直接运行 OpenList。
# orkestr 丢弃了 CAP_CHOWN、CAP_SETGID、CAP_SETUID，chown 和 su-exec 无法工作。

umask ${UMASK:-022}

if [ "$1" = "version" ]; then
  ./openlist version
else
  # Check file of /opt/openlist/data permissions for current user
  # 检查当前用户是否有当前目录的写和执行权限
  if [ -d ./data ]; then
    if ! [ -w ./data ] || ! [ -x ./data ]; then
  cat <<EOF
Error: Current user does not have write and/or execute permissions for the ./data directory: $(pwd)/data
Please visit https://doc.oplist.org/guide/installation/docker#for-version-after-v4-1-0 for more information.
错误：当前用户没有 ./data 目录（$(pwd)/data）的写和/或执行权限。
请访问 https://doc.oplist.org/guide/installation/docker#v4-1-0-%E4%BB%A5%E5%90%8E%E7%89%88%E6%9C%AC 获取更多信息。
Exiting...
EOF
      exit 1
    fi
  fi

  # Define the target directory path for aria2 service
  ARIA2_DIR="/opt/service/start/aria2"
  if [ "$RUN_ARIA2" = "true" ]; then
    # If aria2 should run and target directory doesn't exist, copy it
    if [ ! -d "$ARIA2_DIR" ]; then
      mkdir -p "$ARIA2_DIR"
      cp -r /opt/service/stop/aria2/* "$ARIA2_DIR" 2>/dev/null
    fi
    runsvdir /opt/service/start &
  else
    # If aria2 should NOT run and target directory exists, remove it
    if [ -d "$ARIA2_DIR" ]; then
      rm -rf "$ARIA2_DIR"
    fi
  fi

  # Start Hubu WebDAV Gateway if configured
  # 若配置了 HUBU 环境变量，自动在后台启动 WebDAV 网关服务
  if [ -n "$HUBU_COOKIE" ] && [ -n "$HUBU_DAV_USER" ] && [ -n "$HUBU_DAV_PASS" ]; then
    echo "[Hubu] Starting Hubu WebDAV Gateway on ${HUBU_HOST:-127.0.0.1}:${HUBU_PORT:-8080}..."
    export HUBU_CFG_DIR="${HUBU_CFG_DIR:-/opt/openlist/data/hubu}"
    mkdir -p "$HUBU_CFG_DIR"
    python3 /opt/openlist/hubu_dav.py serve \
      --host "${HUBU_HOST:-127.0.0.1}" \
      --port "${HUBU_PORT:-8080}" \
      --user "${HUBU_LOCAL_USER:-$HUBU_DAV_USER}" \
      --pass "${HUBU_LOCAL_PASS:-$HUBU_DAV_PASS}" 2>&1 | tee -a "${HUBU_CFG_DIR}/hubu_dav.log" &
    echo "[Hubu] Hubu WebDAV Gateway started (PID: $!)."
  else
    echo "[Hubu] Notice: HUBU_COOKIE / HUBU_DAV_USER / HUBU_DAV_PASS not configured, WebDAV Gateway auto-start skipped."
  fi

  exec ./openlist server --no-prefix
fi
