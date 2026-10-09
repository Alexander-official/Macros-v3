# ⚡ Macros v3

**by [@alexvpns](https://t.me/alexvpns)** — Telegram

Marzban / 3x-ui abonelik (subscription) yönetim ve birleştirme paneli. Müşterinin mevcut sub linkini proxyler; harici kaynakları, birden fazla paneli ve kendi node'larını tek, sıralı, isimlendirilmiş bir abonelikte birleştirir. Uygulamaya göre düz liste ya da tam **Xray JSON** verir.

---

## Özellikler

### Abonelik
- **Sub proxy** — `https://domain/macros?url=<kaynak-sub>` ya da kısa token linki `/macros/<token>`.
- **Eski linkler bozulmaz** — `EXTRA_SUB_PATHS` ile ek yollar tanımlanır.
- **Cihaz limiti** — sub başına cihaz sayısı, global aç/kapa, zorunlu limit.
- **UA kara/beyaz liste** — istenmeyen istemcileri engelle.
- **İzinli paneller** — yalnızca belirlenen panellerin linkleri proxylenir.
- **Announce / profil başlıkları** korunur.

### Kaynaklar ve slotlar
- **Harici sub kaynakları** — URL ya da yerel (Local) kaynak; otomatik çekme.
- **📍 Kaynak Sub bloğu** — abonenin kendi kodları, istediğin konuma sürüklenir.
- **🔗 Panel blokları** — entegre panellerdeki kodlar ayrı blok olarak konumlanır.
- **Sürükle-bırak slotlar**, slot bazlı protokol filtresi ve sıralama.
- **⚡ Gerçek gecikme testi** — Xray ile; ölü kodlar ve **ms limitinin** üstündekiler alınmaz (global ya da slot başına).
- **👁 Final önizleme** — birleşik + sıralı son çıktı.

### Adlandırma
- Bayrak + ülke ile otomatik isim, slot başına aç/kapa.
- VLESS WS için CDN etiketi (Google / CloudFront / Fastly).
- Doğrudan node'lar için cloud etiketi (Azure / AWS / Google / Oracle) — metinler özelleştirilebilir.

### JSON abonelik
- Seçilen uygulamalara (Happ, v2rayN, v2rayNG …) tam **Xray JSON** config.
- Direct siteler/IP'ler, reklam engelleme, **otomatik balancer** (leastPing).
- İstemci profilleri: uygulamaya göre format ve protokol filtresi.

### Çoklu panel entegrasyonu
- Birden fazla **Marzban** ve **3x-ui** paneli. Ana panel otomatik algılanır (`.env`'deki Marzban).
- Ana paneldeki kullanıcılar ikincil 3x-ui panellerde **tüm inbound'larla** otomatik açılır.
- Limit ve süre ana panelden gelir; **trafik tüm panellerin toplamıdır**.
- Manuel/otomatik senkron, durum kartları, ayrıntılı rapor (panelin kendi / eşleşen / macros'un eklediği / bu senkronda eklenen).
- **Güvenli:** senkron hiçbir kullanıcıyı silmez; hata olursa loglar ve bildirir.
- Panel kaldırılırken yalnızca macros'un açtığı kullanıcılar silinir; panelin kendi kullanıcılarına dokunulmaz.

### Arayüz
- 9 tema + **💧 Liquid Glass** (gerçek kenar kırılması, renk/bulanıklık/köşe vb. ayarlanabilir).
- 4 dil: TR / EN / RU / TM.
- İstatistik ve log sayfaları, katlanabilir/sabitlenebilir ayar kutuları, JSON ayar dışa/içe aktarma.

---

## Kurulum (otomatik)

Gereken: Ubuntu 22.04+ / Debian 12+, root erişimi, sunucuya yönlendirilmiş bir domain.

```bash
# Dosyaları sunucuya kopyalayın (ör. /opt/macros), sonra:
cd /opt/macros
chmod +x install.sh
sudo ./install.sh
```

Sihirbaz şunları sorar ve kendisi kurar:
- Domain ve SSL e-postası (Let's Encrypt otomatik).
- Admin kullanıcı adı ve şifresi.
- Ana Marzban paneli (opsiyonel).
- Ek sub yolu (opsiyonel).

Kurduğu bileşenler: Python ortamı, Xray-core, systemd servisi (`macros2`), Nginx.

- **Panel:** `https://<domain>/panel/login`
- **Sub linki:** `https://<domain>/macros?url=<kaynak-sub-linki>`

## Güncelleme

```bash
cd /opt/macros
./backup.sh                              # önce yedek
# yeni dosyaları kopyalayın (.env ve macros.db'ye dokunmayın)
./venv/bin/pip install -r requirements.txt
systemctl restart macros2
```

## Yapılandırma

| Dosya | İçerik |
|---|---|
| `.env` | Admin girişi, ana Marzban paneli, `EXTRA_SUB_PATHS`, opsiyonel `BOT_TOKEN` / `BOT_CHAT_ID`. Gizlidir, paylaşılmaz. |
| `macros.db` | Tüm panel ayarları ve müşteri verisi (SQLite). Panelden yönetilir. |

Opsiyonel Telegram botu (`bot.py`): servis izleme, otomatik yeniden başlatma, yedek gönderimi.

## Yedekleme

```bash
./backup.sh      # main.py + macros.db + templates + static + .env -> backups/
```

## Güvenlik

- `macros.db` müşteri verisi, `.env` şifre içerir — ikisi de asla paylaşılmamalı (`.gitignore`'dadır).
- Admin şifresini güçlü seçin.
- Senkron varsayılan olarak hiçbir kullanıcıyı silmez.

## Lisans ve destek

Ticari yazılımdır — bkz. [LICENSE](LICENSE). Lisans, kurulum ve destek: **Telegram [@alexvpns](https://t.me/alexvpns)**
