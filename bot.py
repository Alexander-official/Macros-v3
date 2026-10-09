import asyncio
import httpx
import os
import subprocess
import shutil
import time
from datetime import datetime

# --- AYARLAR (.env'den okunur: BOT_TOKEN, BOT_CHAT_ID) ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
def _load_env():
    p = os.path.join(APP_DIR, ".env")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
_load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("BOT_CHAT_ID", "")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "macros2.service")
MSG_ID_FILE = os.path.join(APP_DIR, "live_msg_id.txt")
INTERVAL_FILE = os.path.join(APP_DIR, "live_interval.txt")
# ---------------

async def send_msg(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
            return r.json().get("result", {}).get("message_id")
    except: return None

async def send_doc(doc_path, caption=""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    try:
        with open(doc_path, "rb") as f:
            async with httpx.AsyncClient(timeout=60) as client:
                await client.post(url, data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"document": f})
    except Exception as e:
        await send_msg(f"❌ Dosya gönderilemedi: `{e}`")

def get_restart_count():
    try:
        out = subprocess.run(["systemctl", "show", "-p", "NRestarts", SERVICE_NAME], capture_output=True, text=True).stdout
        return int(out.strip().split("=")[1])
    except: return 0

def get_system_status():
    try:
        mem = subprocess.run("free -m | awk 'NR==2{printf \"%.1f%% (%sMB / %sMB)\", $3*100/$2, $3, $2 }'", shell=True, capture_output=True, text=True).stdout.strip()
        disk = subprocess.run("df -h / | awk '$NF==\"/\"{printf \"%s (%s / %s)\", $5, $3, $2}'", shell=True, capture_output=True, text=True).stdout.strip()
        cpu = subprocess.run("top -bn1 | grep load | awk '{printf \"%.2f\", $(NF-2)}'", shell=True, capture_output=True, text=True).stdout.strip()
        serv = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True).stdout.strip()
        restarts = get_restart_count()
        
        status_icon = "🟢" if serv == "active" else "🔴"
        return f"{status_icon} *Servis Durumu:* `{serv}`\n🔄 *Crash Sayısı:* `{restarts}`\n🧠 *RAM:* `{mem}`\n💾 *Disk:* `{disk}`\n⚙️ *CPU Yükü:* `{cpu}`"
    except:
        return "Durum alınamadı."

async def create_and_send_zip(caption):
    zip_name = f"/tmp/macros_backup_{int(time.time())}.zip"
    cmd = f'cd {APP_DIR} && zip -r {zip_name} . -x "venv/*" -x "__pycache__/*" -x "*.zip" -x "*.sock"'
    subprocess.run(cmd, shell=True, capture_output=True)
    await send_doc(zip_name, caption=caption)
    if os.path.exists(zip_name):
        os.remove(zip_name)

async def live_dashboard():
    while True:
        try:
            sys_stat = get_system_status()
            logs = subprocess.run(["journalctl", "-u", SERVICE_NAME, "-n", "8", "--no-pager"], capture_output=True, text=True).stdout.strip()
            now = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
            
            # Güncelleme saniyesini dosyadan oku
            interval = 15
            if os.path.exists(INTERVAL_FILE):
                with open(INTERVAL_FILE, "r") as f:
                    val = f.read().strip()
                    if val.isdigit():
                        interval = max(3, int(val)) # Minimum 3 saniye olsun ki ban yemeyelim

            text = f"⚡ *MACROS2 CANLI KONTROL PANELİ* ⚡\n⏳ _Son Güncelleme: {now} (Her {interval}s'de bir)_\n\n{sys_stat}\n\n📝 *Son 8 Log:*\n```text\n{logs[-800:]}\n```\n\n_Komutlar: /live, /setlive [sn], /backup, /restart_"
            
            msg_id = None
            if os.path.exists(MSG_ID_FILE):
                with open(MSG_ID_FILE, "r") as f:
                    content = f.read().strip()
                    if content.isdigit():
                        msg_id = int(content)

            if msg_id:
                url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post(url, data={"chat_id": CHAT_ID, "message_id": msg_id, "text": text, "parse_mode": "Markdown"})
                    if not r.json().get("ok") and "message to edit not found" in r.text:
                        msg_id = None
            
            if not msg_id:
                new_id = await send_msg(text)
                if new_id:
                    with open(MSG_ID_FILE, "w") as f:
                        f.write(str(new_id))
        except: pass
        await asyncio.sleep(interval) # Ayarlanan saniye kadar bekle

async def monitor_service():
    last_restarts = get_restart_count()
    while True:
        try:
            status = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True).stdout.strip()
            current_restarts = get_restart_count()

            if status == "failed" or status == "inactive":
                logs = subprocess.run(["journalctl", "-u", SERVICE_NAME, "-n", "20", "--no-pager"], capture_output=True, text=True).stdout.strip()
                await send_msg(f"🚨 *KRİTİK HATA! Macros2 Çöktü!*\n\n*Otomatik kurtarma başlatılıyor...*\n\n*Son Loglar:*\n```text\n{logs[-1000:]}\n```")
                subprocess.run(["systemctl", "restart", SERVICE_NAME])
                await asyncio.sleep(5)
                last_restarts = get_restart_count()
            
            elif current_restarts > last_restarts:
                last_restarts = current_restarts
        except: pass
        await asyncio.sleep(10)

async def backup_routine():
    while True:
        await asyncio.sleep(1800) # Her 30 dakikada bir
        try:
            await create_and_send_zip(f"📦 *Otomatik Tam Sistem Yedeği*\n⏰ Tarih: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
        except: pass

async def command_listener():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(url, params={"offset": offset, "timeout": 30})
                data = resp.json()
                if data.get("ok"):
                    for req in data["result"]:
                        offset = req["update_id"] + 1
                        msg = req.get("message", {})
                        text = msg.get("text", "")
                        chat = msg.get("chat", {}).get("id", "")
                        
                        if str(chat) == str(CHAT_ID):
                            if text == "/restart":
                                await send_msg("🔄 *Macros2* manuel olarak yeniden başlatılıyor...")
                                subprocess.run(["systemctl", "restart", SERVICE_NAME])
                                await send_msg("✅ Yeniden başlatıldı.")
                            elif text == "/backup":
                                await send_msg("⏳ *Tam klasör yedeği* hazırlanıyor (zip), lütfen bekleyin...")
                                await create_and_send_zip("📦 *Manuel Tam Sistem Yedeği (Zip)*")
                            elif text == "/live":
                                if os.path.exists(MSG_ID_FILE):
                                    os.remove(MSG_ID_FILE)
                            elif text.startswith("/setlive"):
                                parts = text.split()
                                if len(parts) > 1 and parts[1].isdigit():
                                    new_interval = max(3, int(parts[1]))
                                    with open(INTERVAL_FILE, "w") as f:
                                        f.write(str(new_interval))
                                    await send_msg(f"⏱ *Canlı Yenileme Süresi:* Artık her `{new_interval}` saniyede bir güncellenecek!")
                                else:
                                    await send_msg("Yanlış kullanım. Örnek: `/setlive 5`")
        except: pass
        await asyncio.sleep(2)

async def main():
    await asyncio.gather(
        live_dashboard(),
        monitor_service(),
        backup_routine(),
        command_listener()
    )

if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN ve BOT_CHAT_ID .env içinde tanımlı olmalı.")
    asyncio.run(main())
