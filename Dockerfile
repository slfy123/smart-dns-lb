FROM python:3.12-slim
ARG INSTALL_MIHOMO=1
ARG MIHOMO_VERSION=latest
WORKDIR /opt/smart-dns-lb
COPY scripts/download_mihomo.py ./scripts/download_mihomo.py
RUN if [ "$INSTALL_MIHOMO" = 1 ]; then python scripts/download_mihomo.py --output /usr/local/bin/mihomo --version "$MIHOMO_VERSION"; fi
COPY requirements.lock.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app.py config.example.json ./
COPY smart_dns ./smart_dns
COPY templates ./templates
VOLUME ["/var/lib/smart-dns-lb"]
ENV SMART_DNS_DATA_DIR=/var/lib/smart-dns-lb PYTHONUNBUFFERED=1
EXPOSE 53/udp 53/tcp 8080/tcp
CMD ["python", "app.py"]
