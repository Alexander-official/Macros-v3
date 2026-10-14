# ⚡ Macros v3

**by [@alexvpns](https://t.me/alexvpns)** · Telegram

> 🇹🇷 Türkçe · [🇬🇧 English](#-english)

Marzban ve 3x-ui için **abonelik (sub) birleştirme ve yönetim paneli**.

Müşterinizin mevcut sub linkini kendi alan adınız üzerinden sunar ve şunları tek, sıralı, düzgün isimlendirilmiş bir abonelikte birleştirir:
- müşterinin kendi kodları,
- diğer panellerinizdeki kodları,
- harici kaynaklar.

Uygulamaya göre düz liste ya da tam **Xray JSON** verir.

```
Müşteri (Happ / v2rayNG / …)
        │  https://sub.domain.com/macros?url=<asıl-sub-linki>
        ▼
   ⚡ Macros v3 ──► Ana panel (Marzban)   : müşterinin kendi kodları
        │       ├─► Diğer paneller (3x-ui) : aynı müşterinin oradaki kodları
        │       └─► Harici sub'lar          : filtre + ping + fallback
        ▼
  Birleşik, sıralı, isimlendirilmiş abonelik (liste veya Xray JSON)
```

---

## İçindekiler
1. [Özellikler](#özellikler)
2. [Gereksinimler](#gereksinimler)
3. [Kurulum](#kurulum)
4. [İlk ayarlar](#ilk-ayarlar)
5. [Güncelleme](#güncelleme)
6. [Yapılandırma (.env)](#yapılandırma-env)
7. [Dosya yapısı](#dosya-yapısı)
8. [Sık sorulanlar](#sık-sorulanlar)
9. [Kaldırma](#kaldırma)
10. [Lisans ve destek](#lisans-ve-destek)

---

## Özellikler

### 🔗 Abonelik
- Linkler `/macros?url=<kaynak-sub>` ya da kısa token ile `/macros/<token>`.
- Eski linkler bozulmaz: `EXTRA_SUB_PATHS` ile ek yollar açılır.
- Cihaz limiti (sub başına, global, zorunlu).
- User-Agent kara/beyaz listesi.
- İzinli paneller listesi.
- Announce ve profil başlıkları korunur.

### 🧩 Slotlar (sürükle-bırak)

| Slot | Ne yapar |
|---|---|
| **Harici sub** | URL'den kod çeker. Protokol filtresi, ⚡ gerçek gecikme testi ve ms limiti uygulanır. |
| **↪ Fallback** | Ana sub engellenirse ya da kodlarının hiçbiri çalışmazsa yedek linke geçer. |
| **📱 UA / 🆔 HWID** | Kaynağı Happ / v2rayNG / Hiddify gibi çeker (`x-hwid`). HWID 🎲 ile rastgele üretilebilir. |
| **Local** | Elle yapıştırılan kodlar. |
| **📍 Kaynak Sub** | Abonenin kendi kodları; konumu seçilir. |
| **🔗 Panel** | Entegre paneldeki kodlar ayrı blok olarak gelir. |

Ayrıca:
- Her slotun kendi protokol sıralaması olabilir.
- Final önizleme, birleşik son çıktıyı gösterir.
- Kaynak Xray JSON verirse otomatik olarak linke çevrilir.

### 🏷️ Otomatik adlandırma
- Bayrak + ülke adı.
- CDN etiketi (Google / CloudFront / Fastly).
- Cloud etiketi (Azure / AWS / Google / Oracle).
- Etiket metinleri ayarlanabilir, slot başına açılıp kapatılabilir.

### 📦 JSON abonelik
- Seçilen uygulamalara tam Xray config verilir.
- İçinde direct siteler/IP'ler, reklam engelleme ve otomatik balancer (leastPing) bulunur.

### 🔄 Çoklu panel (Marzban + 3x-ui)
- Ana panel otomatiktir (`.env`'deki Marzban).
- Ana paneldeki kullanıcılar 3x-ui panellerinde **tüm inbound'larla** açılır.
- Limit ve süre ana panelden gelir; trafik tüm panellerin **toplamıdır**.
- Senkron kullanıcı **silmez**; hata olursa loglar.
- Panel kaldırılırken yalnızca Macros'un açtığı kullanıcılar silinir.

### 🎨 Arayüz
- 10 tema, aralarında 💧 **Liquid Glass** (ayarlanabilir).
- 4 dil: TR / EN / RU / TM.
- Telefon uyumlu.
- İstatistik ve log sayfaları.

---

## Gereksinimler
- Ubuntu 22.04+ veya Debian 12+, **root** erişimi
- A kaydı bu sunucuya yönlendirilmiş bir **alan adı** (ör. `sub.domain.com`)
- 80 ve 443 portları boş olmalı. Aynı sunucuda başka bir nginx sitesi olabilir.
- En az 1 GB RAM

## Kurulum

```bash
apt update && apt install -y git
git clone https://github.com/Alexander-official/Macros-v3.git /opt/macros
cd /opt/macros
chmod +x install.sh
sudo ./install.sh
```

Sihirbaz sırayla şunları sorar:

| Soru | Örnek |
|---|---|
| Panel domaini | `sub.domain.com` |
| SSL e-postası | `admin@domain.com` |
| Kurulum dizini / port | Enter (varsayılan) |
| Admin kullanıcı / şifre | `admin` / güçlü bir şifre |
| Marzban URL / kullanıcı / şifre | `https://panel.domain.com:8000` (boş bırakılabilir) |
| Ek sub yolu | eski linkleriniz için, yoksa boş |

Kurulum şunları kendisi yapar:
- Python ortamını kurar.
- Xray-core'u indirir.
- `macros2` systemd servisini oluşturur.
- Nginx'i ayarlar.
- Let's Encrypt SSL sertifikasını alır.

Bittiğinde:
- **Panel:** `https://sub.domain.com/panel/login`
- **Müşteri linki:** `https://sub.domain.com/macros?url=https://panel.domain.com:8000/sub/XXXX`

## İlk ayarlar
1. Panele girin. **⚙️ Ayarlar → Harici Sub Kaynakları** bölümünde **📍 Kaynak Sub Bloğu** ekleyin; müşterinin kendi kodları buraya gelir.
2. Harici kaynak eklemek için **+ Yeni Kaynak**: URL, gerekirse fallback, UA ve HWID girin.
3. Diğer panelleriniz için **Entegre Paneller** bölümünü kullanın. 3x-ui eklerken API token gerekir: *3x-ui → Ayarlar → Güvenlik → API Token*.
4. **💾 Kaydet**. Ardından **👁 Final Önizleme** ile çıktıyı kontrol edin.

## Güncelleme
```bash
cd /opt/macros
./backup.sh                         # yedek: backups/
git pull
./venv/bin/pip install -r requirements.txt
systemctl restart macros2
```
`.env` ve `macros.db` güncellemeden etkilenmez.

## Yapılandırma (.env)

| Değişken | Açıklama |
|---|---|
| `ADMIN_USER`, `ADMIN_PASS` | Panel girişi. Şifre boşsa her açılışta rastgele bir şifre üretilip loga yazılır. |
| `MARZBAN_URL`, `MARZBAN_USER`, `MARZBAN_PASS` | Ana panel (opsiyonel) |
| `EXTRA_SUB_PATHS` | Ek sub yolları, virgülle (ör. `mysub,eski`) |
| `BOT_TOKEN`, `BOT_CHAT_ID` | Opsiyonel Telegram izleme/yedek botu (`bot.py`) |

Değişiklikten sonra: `systemctl restart macros2`

## Dosya yapısı
```
main.py            Uygulama (FastAPI)
templates/         Panel sayfaları
static/            Liquid Glass + mobil stiller
install.sh         Otomatik kurulum
backup.sh          Yedekleme
bot.py             Opsiyonel Telegram botu
healthcheck.sh     Opsiyonel bekçi (yanıt vermezse yeniden başlatır)
.env.example       Örnek ayar dosyası
```
`macros.db` (veritabanı) ve `.env` (şifreler) kurulumda oluşturulur. İkisi de repoda **yoktur**.

## Sık sorulanlar

**Servis durumu / loglar?**
```bash
systemctl status macros2
journalctl -u macros2 -f
```

**Şifremi unuttum.**
`.env` içindeki `ADMIN_PASS`'ı değiştirin, sonra `systemctl restart macros2`.

**SSL alınamadı.**
Domainin bu sunucuya yönlendiğinden emin olun, sonra: `certbot --nginx -d sub.domain.com`

**Ping testi çalışmıyor.**
`xray/xray` dosyası var mı kontrol edin. Yoksa `install.sh`'ı tekrar çalıştırın.

**Gecikme kimden ölçülüyor?**
Sunucudan; kullanıcının telefonundan ölçülen değer farklı olabilir.

## Kaldırma
```bash
systemctl disable --now macros2
rm /etc/systemd/system/macros2.service /etc/nginx/sites-enabled/macros2 /etc/nginx/sites-available/macros2
systemctl restart nginx
rm -rf /opt/macros        # önce ./backup.sh ile yedek alın
```

## Lisans ve destek
Ticari yazılımdır: [LICENSE](LICENSE). Kaynak kodu inceleme amacıyla açıktır; satış ve dağıtım izne tabidir.
Lisans, kurulum ve destek için: **Telegram [@alexvpns](https://t.me/alexvpns)**

---

## 🇬🇧 English

**Macros v3** is a subscription merge & management panel for **Marzban** and **3x-ui**.

It serves your customers' existing sub links through your own domain and merges into one sorted, well-named subscription:
- the customer's own nodes,
- nodes from your other panels,
- external sources.

Output is a plain list or full **Xray JSON**, depending on the client app.

**Highlights**
- Drag-and-drop slots: external sources with real Xray latency test, ms limit, **fallback sub**, custom **User-Agent + HWID**.
- Source / panel blocks; per-slot sorting; final preview.
- Auto naming with flag/country, CDN and cloud labels.
- Xray JSON output with routing, ad-block and auto balancer.
- Multi-panel sync: users mirrored to 3x-ui on all inbounds, limits from the main panel, traffic summed. Never deletes users.
- 10 themes including 💧 Liquid Glass; 4 languages; mobile-friendly.

**Install** (Ubuntu 22.04+ / Debian 12+, root, a domain pointing to the server):
```bash
apt update && apt install -y git
git clone https://github.com/Alexander-official/Macros-v3.git /opt/macros
cd /opt/macros && chmod +x install.sh && sudo ./install.sh
```
- **Panel:** `https://<domain>/panel/login`
- **Sub link:** `https://<domain>/macros?url=<original-sub-link>`

**Update:**
```bash
./backup.sh && git pull && ./venv/bin/pip install -r requirements.txt && systemctl restart macros2
```

**License:** commercial, source-available. See [LICENSE](LICENSE).
**Support:** Telegram [@alexvpns](https://t.me/alexvpns)
