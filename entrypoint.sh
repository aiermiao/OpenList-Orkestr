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

  # Start Aria2 Pro daemon if enabled (default: true)
  # 启动内置的 Aria2 Pro 守护进程（默认启用，支持 BT / 磁力链接离线下载）
  # 注意：必须仅监听 127.0.0.1，避免被 Orkestr 端口扫描误识别为主服务端口
  if [ "${ENABLE_ARIA2:-true}" != "false" ]; then
    echo "[Aria2] Starting Aria2 Pro daemon on 127.0.0.1:${ARIA2_PORT:-6800}..."
    ARIA2_DATA_DIR="${ARIA2_DATA_DIR:-/opt/openlist/data/aria2}"
    ARIA2_TEMP_DIR="${ARIA2_TEMP_DIR:-/opt/openlist/data/temp}"
    mkdir -p "$ARIA2_DATA_DIR" "$ARIA2_TEMP_DIR"
    touch "$ARIA2_DATA_DIR/aria2.session"

    # 清理历史未完成的残留临时下载，防止磁盘被历史大文件撑爆超限 (Disk over the limit)
    if [ "${CLEAN_TEMP_ON_START:-true}" = "true" ]; then
      echo "[Cleanup] Cleaning up stale temporary files in $ARIA2_TEMP_DIR..."
      rm -rf "$ARIA2_TEMP_DIR"/* 2>/dev/null || true
    fi

    # 内置高质量 BT Trackers，秒级启动不阻塞应用
    DEFAULT_TRACKERS="udp://tracker.opentrackr.org:1337/announce,udp://open.tracker.cl:1337/announce,udp://tracker.openbittorrent.com:80/announce,udp://opentracker.i2p.rocks:6969/announce,udp://open.demonii.com:1337/announce,udp://exodus.desync.com:6969/announce,udp://tracker.torrent.eu.org:451/announce,udp://explodie.org:6969/announce,udp://open.stealth.si:80/announce,udp://tracker.tiny-vps.com:6969/announce"

    # 生成 Aria2 Pro 优化配置文件
    cat > "$ARIA2_DATA_DIR/aria2.conf" <<EOF
## RPC 设置 (仅监听本地回环 127.0.0.1) ##
enable-rpc=true
rpc-listen-all=false
rpc-listen-port=${ARIA2_PORT:-6800}
rpc-allow-origin-all=true
${ARIA2_SECRET:+rpc-secret=$ARIA2_SECRET}

## 存储与会话 ##
dir=$ARIA2_TEMP_DIR
input-file=$ARIA2_DATA_DIR/aria2.session
save-session=$ARIA2_DATA_DIR/aria2.session
save-session-interval=30
file-allocation=none
disk-cache=16M

## 下载线程与连接数 ##
max-concurrent-downloads=5
max-connection-per-server=16
min-split-size=4M
split=16

## BT / 磁力链接优化 (Aria2 Pro) ##
enable-dht=true
enable-dht6=true
dht-listen-port=6881-6999
dht-file-path=$ARIA2_DATA_DIR/dht.dat
dht-file-path6=$ARIA2_DATA_DIR/dht6.dat
enable-peer-exchange=true
bt-enable-lpd=true
bt-max-peers=60
bt-save-metadata=true
follow-torrent=mem
check-integrity=true
seed-time=0
seed-ratio=0.1
peer-id-prefix=-TR2770-
user-agent=Transmission/2.77
bt-tracker=$DEFAULT_TRACKERS
EOF

    aria2c --conf-path="$ARIA2_DATA_DIR/aria2.conf" -D --log="$ARIA2_DATA_DIR/aria2.log" --log-level=warn
    echo "[Aria2] Aria2 Pro daemon started on 127.0.0.1:${ARIA2_PORT:-6800}."

    # 后台异步拉取全量最新 Tracker 列表并热更新，绝不阻塞应用启动
    (
      NEW_TRACKERS=$(curl -sSL --connect-timeout 4 -m 10 "https://trackerslist.com/best_aria2.txt" 2>/dev/null)
      if [ -n "$NEW_TRACKERS" ]; then
        sed -i "s|^bt-tracker=.*|bt-tracker=$NEW_TRACKERS|" "$ARIA2_DATA_DIR/aria2.conf"
      fi
    ) &
  fi

  # Start Hubu WebDAV Gateway if configured
  # 若配置了 HUBU 环境变量，自动在后台启动 WebDAV 网关服务（仅监听 127.0.0.1）
  if [ -n "$HUBU_COOKIE" ] && [ -n "$HUBU_DAV_USER" ] && [ -n "$HUBU_DAV_PASS" ]; then
    echo "[Hubu] Starting Hubu WebDAV Gateway on ${HUBU_HOST:-127.0.0.1}:${HUBU_PORT:-8080}..."
    export HUBU_CFG_DIR="${HUBU_CFG_DIR:-/opt/openlist/data/hubu}"
    mkdir -p "$HUBU_CFG_DIR"
    python3 -u /opt/openlist/hubu_dav.py serve \
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
