FROM openlistteam/openlist:latest

USER root

# 安装 Python3、pip 与网络工具 curl
RUN apk add --no-cache python3 py3-pip curl && \
    pip install --no-cache-dir --break-system-packages requests wsgidav cheroot

WORKDIR /opt/openlist/

# 拷贝自定义 Hubu 脚本
COPY hubu_client.py hubu_dav.py ./
RUN chmod +x hubu_client.py hubu_dav.py && \
    ln -sf /opt/openlist/hubu_client.py /usr/local/bin/hubu-client && \
    ln -sf /opt/openlist/hubu_dav.py /usr/local/bin/hubu-dav

# 拷贝并设置启动脚本
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV UMASK=022
VOLUME /opt/openlist/data/
EXPOSE 5244 5245 8080

ENTRYPOINT [ "/entrypoint.sh" ]
