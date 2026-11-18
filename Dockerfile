FROM openlistteam/openlist:latest

USER root

# 安装 Python3、pip、curl 与 aria2 并清理包缓存
RUN apk add --no-cache python3 py3-pip curl aria2 && \
    pip install --no-cache-dir --break-system-packages requests wsgidav cheroot && \
    rm -rf /var/cache/apk/* /tmp/* /root/.cache

WORKDIR /opt/openlist/

# 避免平台构建缓存旧脚本
ARG CACHEBUST=20261119_v2

# 拷贝自定义 Hubu 脚本
COPY hubu_client.py hubu_dav.py ./
RUN chmod +x hubu_client.py hubu_dav.py && \
    ln -sf /opt/openlist/hubu_client.py /usr/local/bin/hubu-client && \
    ln -sf /opt/openlist/hubu_dav.py /usr/local/bin/hubu-dav

# 拷贝并设置启动脚本
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV UMASK=022
ENV ENABLE_ARIA2=true RUN_ARIA2=true PORT=5244 PYTHONUNBUFFERED=1
VOLUME /opt/openlist/data/
EXPOSE 5244

ENTRYPOINT [ "/entrypoint.sh" ]
