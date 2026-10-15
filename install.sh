#!/bin/bash
# Macros v3 — otomatik kurulum / güncelleme (Ubuntu/Debian). by @alexvpns (Telegram)
#
# Bu sunucuda zaten bir Macros varsa (eski sürüm dahil) KURULUM yerine GÜNCELLEME yapar:
#   - veritabanı (macros.db) ve .env'e DOKUNMAZ, sadece eksik ayarları .env'e ekler,
#   - önce kod + veritabanı yedeği alır, yeni sürüm açılmazsa eski koda otomatik geri döner,
#   - nginx / SSL ayarlarını değiştirmez.
set -e

if [ "$(id -u)" != "0" ]; then echo "❌ Root olarak çalıştırın: sudo ./install.sh"; exit 1; fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
XRAY_VERSION="v26.3.27"
SERVICE="${MACROS_SERVICE:-macros2}"
UNIT="/etc/systemd/system/$SERVICE.service"
NEW_VERSION="$(sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' "$SRC_DIR/main.py" | head -1)"

echo "=================================================="
echo "        ⚡ Macros v3 — Kurulum Sihirbazı ⚡"
echo "              by @alexvpns (Telegram)"
echo "=================================================="

# ---------- Ortak yardımcılar ----------
env_get(){ [ -f "$1/.env" ] && sed -n "s/^$2=//p" "$1/.env" | tail -1 || true; }
env_has(){ [ -f "$1/.env" ] && grep -q "^$2=" "$1/.env"; }
env_add(){ # .env'de anahtar yoksa ekler (var olanı ASLA değiştirmez)
  env_has "$1" "$2" || echo "$2=$3" >> "$1/.env"
}

install_xray(){ # $1 = hedef dizin. Sadece gecikme (ping) testi için kullanılır.
  local dir="$1" cur=""
  [ -x "$dir/xray/xray" ] && cur="$("$dir/xray/xray" version 2>/dev/null | head -1 | awk '{print $2}')"
  if [ "v$cur" = "$XRAY_VERSION" ]; then echo "[*] Xray-core zaten $XRAY_VERSION"; return; fi
  echo "[*] Xray-core $XRAY_VERSION indiriliyor..."
  case "$(uname -m)" in
    x86_64|amd64) XARCH="64" ;;
    aarch64|arm64) XARCH="arm64-v8a" ;;
    *) echo "⚠️  Desteklenmeyen mimari: $(uname -m) — ping testi çalışmayabilir."; XARCH="64" ;;
  esac
  mkdir -p "$dir/xray"
  if curl -fsSL -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/Xray-linux-$XARCH.zip"; then
    unzip -o -q /tmp/xray.zip -d "$dir/xray" && chmod +x "$dir/xray/xray"
  else
    echo "⚠️  Xray indirilemedi — panel çalışır, sadece ping testi kapalı kalır."
  fi
  rm -f /tmp/xray.zip
}

write_unit(){ # $1 = dizin, $2 = port
  cat > "$UNIT" <<SVC_EOF
[Unit]
Description=Macros v3
After=network.target

[Service]
User=root
WorkingDirectory=$1
EnvironmentFile=-$1/.env
# Tek worker: oturum + senkron durumu süreç içinde tutulur.
ExecStart=$1/venv/bin/uvicorn main:app --host 127.0.0.1 --port $2 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SVC_EOF
}

wait_healthy(){ # $1 = port. Panel giriş sayfası 200 dönene kadar (en fazla ~40 sn) bekler.
  for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/panel/login" || true)"
    [ "$code" = "200" ] && return 0
    sleep 2
  done
  return 1
}

# ---------- Mevcut kurulum var mı? ----------
OLD_DIR=""
if [ -f "$UNIT" ]; then
  OLD_DIR="$(sed -n 's/^WorkingDirectory=//p' "$UNIT" | head -1)"
fi
[ -z "$OLD_DIR" ] && [ -f /opt/macros2/macros.db ] && [ "$SERVICE" = "macros2" ] && OLD_DIR="/opt/macros2"
[ -z "$OLD_DIR" ] && [ -f "$SRC_DIR/macros.db" ] && OLD_DIR="$SRC_DIR"

if [ -n "$OLD_DIR" ] && [ -f "$OLD_DIR/main.py" ]; then
  # =====================================================================
  #                         GÜNCELLEME MODU
  # =====================================================================
  DIR="$OLD_DIR"
  OLD_VERSION="$(sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' "$DIR/main.py" | head -1)"
  OLD_VERSION="${OLD_VERSION:-eski sürüm}"
  APP_PORT="$( (grep -o -- '--port [0-9]*' "$UNIT" 2>/dev/null || true) | awk '{print $2}' | head -1)"
  APP_PORT="${APP_PORT:-8080}"
  echo ""
  echo "🔎 Bu sunucuda Macros zaten kurulu: $DIR  (sürüm: $OLD_VERSION, port: $APP_PORT)"
  echo "   → GÜNCELLEME yapılacak: $OLD_VERSION → $NEW_VERSION"
  echo "   ✔ macros.db (müşteriler, cihazlar, ayarlar) korunur"
  echo "   ✔ .env (şifreler) korunur — sadece eksik ayarlar eklenir"
  echo "   ✔ nginx / SSL değişmez, eski sub linkleri çalışmaya devam eder"
  if [ -z "$MACROS_YES" ]; then
    read -p "Devam edilsin mi? [E/h]: " OK
    case "$OK" in h|H|n|N|hayir|no) echo "İptal edildi."; exit 0 ;; esac
  fi

  TS="$(date +%Y-%m-%d_%H-%M-%S)"
  mkdir -p "$DIR/backups"

  # 1) Yedek: kod (veritabanı hariç) + veritabanının tutarlı bir kopyası
  echo "[*] Yedek alınıyor..."
  CODE_BAK="$DIR/backups/pre-update_${TS}.tar.gz"
  tar czf "$CODE_BAK" -C "$DIR" --exclude=./venv --exclude=./xray --exclude=./backups --exclude=./masscan \
      --exclude='./macros.db*' --exclude=./__pycache__ . 2>/dev/null || true
  OLD_DB="$(sed -n 's/^DB *= *"\([^"]*\)".*/\1/p' "$DIR/main.py" | head -1)"   # eski sürümlerde DB yolu koda gömülüydü
  DB_PATH="$(env_get "$DIR" MACROS_DB)"; DB_PATH="${DB_PATH:-${OLD_DB:-$DIR/macros.db}}"
  if [ -f "$DB_PATH" ]; then
    python3 - "$DB_PATH" "$DIR/backups/macros.db.pre-update_${TS}" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
    echo "    ✔ veritabanı yedeği: backups/macros.db.pre-update_${TS}"
  fi
  echo "    ✔ kod yedeği: backups/$(basename "$CODE_BAK")"

  # 2) .env: yoksa oluştur; varsa sadece eksikleri ekle
  [ -f "$DIR/.env" ] || { echo "[*] .env yok — oluşturuluyor"; : > "$DIR/.env"; }
  # Eski sürümler ADMIN_PASS yoksa 'changeme123' kullanıyordu; yeni sürüm rastgele üretir.
  # Girişin kopmaması için eski davranış korunur (sonra değiştirmeniz önerilir).
  if [ -z "$(env_get "$DIR" ADMIN_PASS)" ]; then
    sed -i '/^ADMIN_PASS=/d' "$DIR/.env"
    echo "ADMIN_PASS=changeme123" >> "$DIR/.env"; WEAK_PASS=1
  fi
  [ "$(env_get "$DIR" ADMIN_PASS)" = "changeme123" ] && WEAK_PASS=1
  env_add "$DIR" ADMIN_USER admin
  env_add "$DIR" MARZBAN_URL ""
  env_add "$DIR" MARZBAN_USER ""
  env_add "$DIR" MARZBAN_PASS ""
  # Eski sürümde DB yolu koda gömülüyse ve farklı bir yerdeyse, yeni sürüm aynı dosyayı kullansın
  if [ -n "$OLD_DB" ] && [ "$OLD_DB" != "$DIR/macros.db" ] && [ -f "$OLD_DB" ]; then env_add "$DIR" MACROS_DB "$OLD_DB"; fi
  # Eski sürümde koda gömülü sub yolları (ör. /alexander77) → EXTRA_SUB_PATHS (müşteri linkleri bozulmasın)
  OLD_PATHS="$( (grep -oE '@app\.get\("/[A-Za-z0-9_-]+/\{path:path\}"\)' "$DIR/main.py" || true) | sed -E 's#@app\.get\("/([^/]+)/.*#\1#' \
               | (grep -vxE 'macros|panel|static|healthcheck' || true) | sort -u | paste -sd, -)"
  CUR_PATHS="$(env_get "$DIR" EXTRA_SUB_PATHS)"
  if [ -n "$OLD_PATHS" ]; then
    MERGED="$(echo "$CUR_PATHS,$OLD_PATHS" | tr ',' '\n' | sed 's#^/##;s#/$##' | (grep -v '^$' || true) | sort -u | paste -sd, -)"
    sed -i '/^EXTRA_SUB_PATHS=/d' "$DIR/.env"; echo "EXTRA_SUB_PATHS=$MERGED" >> "$DIR/.env"
    echo "    ✔ eski sub yolları korunuyor: /${MERGED//,/ /}"
  else
    env_add "$DIR" EXTRA_SUB_PATHS ""
  fi
  # Eski bot.py'de token koda gömülüyse .env'e taşı (yeni bot.py .env'den okur)
  if [ -f "$DIR/bot.py" ] && [ -z "$(env_get "$DIR" BOT_TOKEN)" ]; then
    OLD_TOK="$(sed -n 's/^TOKEN *= *"\([0-9]\{6,\}:[A-Za-z0-9_-]\{20,\}\)".*/\1/p' "$DIR/bot.py" | head -1)"
    OLD_CHAT="$(sed -n 's/^CHAT_ID *= *"\([-0-9]*\)".*/\1/p' "$DIR/bot.py" | head -1)"
    sed -i '/^BOT_TOKEN=$/d;/^BOT_CHAT_ID=$/d' "$DIR/.env"
    env_add "$DIR" BOT_TOKEN "$OLD_TOK"; env_add "$DIR" BOT_CHAT_ID "$OLD_CHAT"
  fi
  env_add "$DIR" BOT_TOKEN ""; env_add "$DIR" BOT_CHAT_ID ""
  chmod 600 "$DIR/.env"

  # 3) Yeni kodu kopyala (veritabanı, .env, yedekler, venv, xray kopyalanmaz)
  if [ "$DIR" != "$SRC_DIR" ]; then
    echo "[*] Yeni sürüm dosyaları kopyalanıyor → $DIR"
    for f in main.py bot.py backup.sh healthcheck.sh cleanup_db.py install.sh requirements.txt README.md LICENSE .env.example .gitignore; do
      [ -e "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$DIR/$f"
    done
    for d in templates static; do
      mkdir -p "$DIR/$d" && cp -rf "$SRC_DIR/$d/." "$DIR/$d/"
    done
    chmod +x "$DIR"/*.sh 2>/dev/null || true
  fi

  # 4) Python bağımlılıkları + Xray
  echo "[*] Python bağımlılıkları güncelleniyor..."
  python3 -c "import venv, ensurepip" 2>/dev/null || { apt update -y && apt install -y python3 python3-venv python3-pip; }
  [ -x "$DIR/venv/bin/pip" ] || python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install --upgrade pip -q || true
  "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt" -q
  command -v unzip >/dev/null || apt install -y unzip
  install_xray "$DIR"

  # 5) Servisi yeniden başlat (veritabanı tabloları açılışta sadece EKLEME ile güncellenir, veri silinmez)
  write_unit "$DIR" "$APP_PORT"
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  echo "[*] Servis kontrol ediliyor..."
  if wait_healthy "$APP_PORT"; then
    echo "=================================================="
    echo "✅ GÜNCELLENDİ: $OLD_VERSION → $NEW_VERSION"
    echo "📂 Dizin:      $DIR"
    echo "🗄️  Veritabanı: korundu ($DB_PATH)"
    echo "💾 Yedekler:   $DIR/backups/"
    [ -n "$WEAK_PASS" ] && echo "⚠️  Panel şifresi zayıf (changeme123). Değiştirin: $DIR/.env → ADMIN_PASS, sonra: systemctl restart $SERVICE"
    echo "💬 Destek:     Telegram @alexvpns"
    echo "=================================================="
  else
    echo "❌ Yeni sürüm açılmadı — eski koda geri dönülüyor (veritabanına dokunulmadı)..."
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    tar xzf "$CODE_BAK" -C "$DIR"
    systemctl restart "$SERVICE"
    if wait_healthy "$APP_PORT"; then echo "↩️  Eski sürüm geri yüklendi ve çalışıyor."
    else echo "⚠️  Eski sürüm de açılmadı: journalctl -u $SERVICE -n 50"; fi
    echo "Yukarıdaki log ile Telegram @alexvpns'e yazabilirsiniz."
    exit 1
  fi
  exit 0
fi

# =====================================================================
#                           YENİ KURULUM
# =====================================================================

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
install_xray "$INSTALL_DIR"

# 7. systemd servisi
echo "[*] Servis oluşturuluyor..."
write_unit "$INSTALL_DIR" "$APP_PORT"
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

wait_healthy "$APP_PORT" || echo "⚠️  Panel henüz yanıt vermiyor: journalctl -u $SERVICE -n 50"

echo "=================================================="
echo "✅ KURULUM TAMAMLANDI — Macros v$NEW_VERSION"
echo "🌐 Panel:     https://$DOMAIN/panel/login"
echo "🔗 Sub linki: https://$DOMAIN/macros?url=<kaynak-sub-linki>"
echo "💬 Destek:    Telegram @alexvpns"
echo "=================================================="
