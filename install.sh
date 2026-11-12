#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
APP_DIR=/opt/smart-dns-lb
DATA_DIR=/var/lib/smart-dns-lb
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=smart-dns-lb.service
[[ $(id -u) -eq 0 ]] || { echo '请使用 root 运行'; exit 1; }
command -v systemctl >/dev/null || { echo '需要 systemd'; exit 1; }

if [[ "${1:-}" == uninstall ]]; then
  [[ $# -eq 1 || ( $# -eq 2 && "$2" == --purge ) ]] || { echo '用法：install.sh uninstall [--purge]'; exit 1; }
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE"
  rm -rf "/etc/systemd/system/$SERVICE.d" "$APP_DIR"
  systemctl daemon-reload
  if [[ "${2:-}" == --purge ]]; then
    rm -rf "$DATA_DIR"
    echo '卸载完成，数据已清理。'
  else
    echo "卸载完成，数据保留在 $DATA_DIR；添加 --purge 可清理数据。"
  fi
  exit 0
fi
[[ $# -eq 0 ]] || { echo '用法：install.sh [uninstall [--purge]]'; exit 1; }
command -v apt-get >/dev/null || { echo '支持 Ubuntu/Debian'; exit 1; }
command -v python3 >/dev/null || { echo '请先安装 Python 3.10+'; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'

was_active=0
completed=0
if systemctl is-active --quiet "$SERVICE"; then
  was_active=1
  systemctl stop "$SERVICE"
fi
cleanup() {
  if [[ "$completed" == 0 && "$was_active" == 1 ]]; then
    echo '升级未完成，尝试恢复已有服务。' >&2
    systemctl start "$SERVICE" || true
  fi
}
trap cleanup EXIT

listen_conf="/etc/systemd/system/$SERVICE.d/listen.conf"
if [[ -z "${DNS_HOST:-}" && -f "$listen_conf" ]]; then
  DNS_HOST="$(sed -n 's/^Environment=SMART_DNS_BIND_HOST=//p' "$listen_conf" | head -n 1)"
fi
DNS_HOST="${DNS_HOST:-0.0.0.0}"
if systemctl is-active --quiet systemd-resolved; then
  echo '若 53 端口冲突：在 /etc/systemd/resolved.conf 的 [Resolve] 中设置 DNSStubListener=no，然后 sudo systemctl restart systemd-resolved。请先确认 /etc/resolv.conf 指向有效上游；脚本保留系统 DNS 配置。'
fi
python3 - "$DNS_HOST" <<'PY'
import ipaddress
import socket
import sys
host = sys.argv[1]
family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    try:
        with socket.socket(family, kind) as sock:
            sock.bind((host, 53))
    except OSError as exc:
        raise SystemExit(f'53 端口冲突：{exc}。请处理占用，或用 DNS_HOST 指定本机网卡地址。systemd-resolved 用户请检查 DNSStubListener=no。')
PY
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ca-certificates
id -u smartdns >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin smartdns
install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o smartdns -g smartdns -m 0700 "$DATA_DIR"
if [[ "$SCRIPT_DIR" != "$APP_DIR" ]]; then
  for file in app.py requirements.txt requirements.lock.txt config.example.json smart-dns-lb.service; do
    install -m 0644 "$SCRIPT_DIR/$file" "$APP_DIR/$file"
  done
  for directory in smart_dns templates scripts; do
    install -d -m 0755 "$APP_DIR/$directory"
    for file in "$SCRIPT_DIR/$directory"/*; do
      if [[ -f "$file" ]]; then install -m 0644 "$file" "$APP_DIR/$directory/"; fi
    done
  done
fi
(umask 022; python3 -m venv "$APP_DIR/.venv"; "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.lock.txt")
chmod -R a+rX "$APP_DIR/.venv"
if [[ "${SKIP_MIHOMO:-0}" != 1 ]]; then
  python3 "$APP_DIR/scripts/download_mihomo.py" --output "$APP_DIR/mihomo" --version "${MIHOMO_VERSION:-latest}"
  (cd "$APP_DIR"; "$APP_DIR/.venv/bin/python" - "$DATA_DIR" "$APP_DIR/mihomo" <<'PY'
import sys
from smart_dns.models import Settings
from smart_dns.storage import Store
store = Store(sys.argv[1])
try:
    existing = store.get_config()
    version, payload = existing if existing else (0, {'settings': Settings().model_dump(), 'nodes': []})
    payload['settings']['mihomo_path'] = sys.argv[2]
    store.save_config(payload, version)
finally:
    store.close()
PY
  )
fi
chown -R smartdns:smartdns "$DATA_DIR"
install -m 0644 "$APP_DIR/smart-dns-lb.service" "/etc/systemd/system/$SERVICE"
install -d -m 0755 "/etc/systemd/system/$SERVICE.d"
cat > "$listen_conf" <<EOF
[Service]
Environment=SMART_DNS_BIND_HOST=$DNS_HOST
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
runuser -u smartdns -- "$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" --data-dir "$DATA_DIR" --show-token >/dev/null
systemctl start "$SERVICE"
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager
  exit 1
fi
completed=1
panel_address="$(cd "$APP_DIR"; "$APP_DIR/.venv/bin/python" - "$DATA_DIR" <<'PY'
import sys
from smart_dns.models import Settings
from smart_dns.storage import Store
store = Store(sys.argv[1])
try:
    config = store.get_config()
    settings = Settings.model_validate(config[1]['settings']) if config else Settings()
    host = settings.api_host
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    authority = '[' + host + ']' if ':' in host else host
    print(f'http://{authority}:{settings.api_port}')
    print(f'ssh -N -L 18080:{authority}:{settings.api_port} root@SERVER')
finally:
    store.close()
PY
)"
echo '安装完成，面板地址与 SSH 隧道命令：'
echo "$panel_address"
echo '管理令牌：'
runuser -u smartdns -- "$APP_DIR/.venv/bin/python" "$APP_DIR/app.py" --data-dir "$DATA_DIR" --show-token
echo '下一步：打开 http://127.0.0.1:18080，填写服务域名和 NS IPv4，导入 VLESS 节点，并在父区配置 NS/glue 后检查委派。'
