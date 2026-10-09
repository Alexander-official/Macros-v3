#!/bin/bash
# Macros v3 — otomatik kurulum (Ubuntu/Debian). by @alexvpns (Telegram)
set -e

if [ "$(id -u)" != "0" ]; then echo "❌ Root olarak çalıştırın: sudo ./install.sh"; exit 1; fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
XRAY_VERSION="v26.3.27"
SERVICE="macros2"

echo "=================================================="
echo "        ⚡ Macros v3 — Kurulum Sihirbazı ⚡"
echo "              by @alexvpns (Telegram)"
echo "=================================================="

# 1. Bilgiler
read -p "🌐 Panel domaini (ör: sub.domain.com): " DOMAIN
[ -z "$DOMAIN" ] && { echo "❌ Domain gerekli."; exit 1; }
read -p "📧 SSL e-posta [admin@$DOMAIN]: " SSL_EMAIL
SSL_EMAIL=${SSL_EMAIL:-admin@$DOMAIN}
read -p "📂 Kurulum dizini [$SRC_DIR]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-$SRC_DIR}
read -p "🔌 Dahili port [8080]: " APP_PORT
APP_PORT=${APP_PORT:-8080}
read -p "👤 Admin kullanıcı adı [admin]: " ADMIN_USER
ADMIN_USER=${ADMIN_USER:-admin}
while true; do
  read -s -p "🔑 Admin şifresi (en az 8 karakter): " ADMIN_PASS; echo ""
  [ ${#ADMIN_PASS} -ge 8 ] && break
  echo "⚠️  Şifre çok kısa."
done
echo ""
echo "— Ana panel (Marzban) — müşteri sub linklerinin geldiği panel. Boş bırakılabilir."
read -p "🛰️  Marzban URL (ör: https://panel.domain.com:8000): " MARZBAN_URL
read -p "🛰️  Marzban kullanıcı: " MARZBAN_USER
read -s -p "🛰️  Marzban şifre: " MARZBAN_PASS; echo ""
read -p "🔗 Ek sub yolu (eski linkler için, ör: mysub — boş = sadece /macros): " EXTRA_SUB_PATHS

# 2. Paketler
echo "[*] Bağımlılıklar kuruluyor..."
apt update -y
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl unzip

# 3. Dosyalar
if [ "$INSTALL_DIR" != "$SRC_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  cp -r "$SRC_DIR"/. "$INSTALL_DIR"/
fi
cd "$INSTALL_DIR"

# 4. Python ortamı
echo "[*] Python ortamı hazırlanıyor..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# 5. .env (gizli — repoya girmez)
if [ -f .env ]; then cp .env ".env.bak.$(date +%s)"; fi
cat > .env <<ENV_EOF
ADMIN_USER=$ADMIN_USER
ADMIN_PASS=$ADMIN_PASS
MARZBAN_URL=${MARZBAN_URL%/}
MARZBAN_USER=$MARZBAN_USER
MARZBAN_PASS=$MARZBAN_PASS
EXTRA_SUB_PATHS=$EXTRA_SUB_PATHS
BOT_TOKEN=
BOT_CHAT_ID=
ENV_EOF
chmod 600 .env

# 6. Xray-core (gerçek gecikme testi için)
echo "[*] Xray-core $XRAY_VERSION indiriliyor..."
case "$(uname -m)" in
  x86_64|amd64) XARCH="64" ;;
  aarch64|arm64) XARCH="arm64-v8a" ;;
  *) echo "⚠️  Desteklenmeyen mimari: $(uname -m) — ping testi çalışmayabilir."; XARCH="64" ;;
esac
mkdir -p xray
curl -sL -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/Xray-linux-$XARCH.zip"
unzip -o -q /tmp/xray.zip -d xray && chmod +x xray/xray && rm -f /tmp/xray.zip

# 7. systemd servisi
echo "[*] Servis oluşturuluyor..."
cat > /etc/systemd/system/$SERVICE.service <<SVC_EOF
[Unit]
Description=Macros v3
After=network.target

[Service]
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$INSTALL_DIR/.env
# Tek worker: oturum + senkron durumu süreç içinde tutulur.
ExecStart=$INSTALL_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port $APP_PORT --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SVC_EOF
systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE

# 8. Nginx + SSL
echo "[*] Nginx ayarlanıyor..."
cat > /etc/nginx/sites-available/$SERVICE <<NGINX_EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
}
NGINX_EOF
ln -sf /etc/nginx/sites-available/$SERVICE /etc/nginx/sites-enabled/
nginx -t && systemctl restart nginx

echo "[*] SSL sertifikası alınıyor..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$SSL_EMAIL" --redirect \
  || echo "⚠️  SSL alınamadı. Domainin bu sunucuya yönlendiğinden emin olup tekrar deneyin: certbot --nginx -d $DOMAIN"

echo "=================================================="
echo "✅ KURULUM TAMAMLANDI — Macros v3"
echo "🌐 Panel:     https://$DOMAIN/panel/login"
echo "🔗 Sub linki: https://$DOMAIN/macros?url=<kaynak-sub-linki>"
echo "💬 Destek:    Telegram @alexvpns"
echo "=================================================="
