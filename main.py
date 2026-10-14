from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import httpx, sqlite3, hashlib, secrets, os, re, base64, threading, json, asyncio, urllib.parse, random, time, shutil, ipaddress, uuid
from contextlib import asynccontextmanager, contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("MACROS_DB", os.path.join(BASE_DIR, "macros.db"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS") or ""
if not ADMIN_PASS:  # varsayilan sifre YOK: .env'de tanimli degilse rastgele uretilir ve loga yazilir
    ADMIN_PASS = secrets.token_urlsafe(12)
    print(f"[MACROS] ⚠ ADMIN_PASS .env'de yok — gecici sifre: {ADMIN_PASS}")
DB_LOCK = threading.Lock()
# Upstream panelden HER ZAMAN duz link listesi almak icin sabit UA (Marzban, Happ gibi UA'lara
# JSON dondurur; biz once link alip formati kendimiz sectigimiz icin notr bir UA kullaniriz).
_UPSTREAM_UA = "v2rayNG/1.8.19"

# --- MARZBAN API AYARLARI ---
# Kimlik bilgileri .env'den okunur (koda gömülü tutulmaz). Eksikse Marzban senkronu
# sessizce devre disi kalir; abonelik (sub) linkleri bundan etkilenmez.
APP_NAME = "Macros"
APP_VERSION = "3.0.0"
AUTHOR_TG = "alexvpns"   # Telegram: @alexvpns
MARZBAN_URL = os.environ.get("MARZBAN_URL", "").rstrip("/")
MARZBAN_USER = os.environ.get("MARZBAN_USER", "")
MARZBAN_PASS = os.environ.get("MARZBAN_PASS", "")
marzban_token = None

async def marzban_login():
    global marzban_token
    if not (MARZBAN_URL and MARZBAN_USER and MARZBAN_PASS):
        return False
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            data = {"grant_type": "password", "username": MARZBAN_USER, "password": MARZBAN_PASS}
            resp = await client.post(f"{MARZBAN_URL}/api/admin/token", data=data)
            if resp.status_code == 200:
                marzban_token = resp.json().get("access_token")
                print("[MARZBAN API] ✅ Başarıyla giriş yapıldı ve Token alındı.")
                return True
            else:
                print(f"[MARZBAN API] ❌ Login Hatası: {resp.status_code} - {resp.text}")
    except Exception as e: 
        print(f"[MARZBAN API] ❌ Login Bağlantı Hatası: {repr(e)}")
    return False

async def marzban_api_request(method, endpoint, json_data=None):
    global marzban_token
    if not marzban_token:
        if not await marzban_login(): return None
    headers = {"Authorization": f"Bearer {marzban_token}", "Accept": "application/json"}
    
    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        try:
            resp = await client.request(method, f"{MARZBAN_URL}{endpoint}", headers=headers, json=json_data)
            if resp.status_code == 401:
                print("[MARZBAN API] ⚠️ Token süresi dolmuş, yeniden login olunuyor...")
                if await marzban_login():
                    headers["Authorization"] = f"Bearer {marzban_token}"
                    resp = await client.request(method, f"{MARZBAN_URL}{endpoint}", headers=headers, json=json_data)
                else: 
                    return None
            
            if resp.status_code not in [200, 201]:
                print(f"[MARZBAN API] ⚠️ İSTEK HATASI ({method} {endpoint}): {resp.status_code} -> {resp.text}")
            return resp
        except Exception as e: 
            print(f"[MARZBAN API] ❌ BAĞLANTI HATASI ({method} {endpoint}): {repr(e)}")
            return None

async def sync_single_marzban_user(username):
    try:
        resp = await marzban_api_request("GET", f"/api/user/{urllib.parse.quote(username)}")
        if not resp or resp.status_code != 200: return
        mz_user = resp.json()
        note = (mz_user.get("note", "") or "").strip()
        
        if not note: return
        
        parts = [p.strip() for p in note.split("|")]
        base_text = parts[0]
        
        if not base_text.isdigit(): return
        
        limit_val = int(base_text)
        
        with db_session() as con:
            local_sub = con.execute("SELECT * FROM subs WHERE name=?", (username,)).fetchone()
            if not local_sub: return
            
            with DB_LOCK:
                try:
                    con.execute("UPDATE subs SET device_limit=?, force_limit=1 WHERE id=?", (limit_val, local_sub["id"]))
                    con.commit()
                except: pass
            
            devs = con.execute("SELECT device_model FROM devices WHERE sub_id=?", (local_sub["id"],)).fetchall()
            dev_count = len(devs)
            
            new_note = str(limit_val)
            if dev_count > 0:
                models = [d["device_model"] or "Bilinmeyen" for d in devs]
                models = [m for m in models if m]
                if len(models) > 15:
                    models = models[:15] + ["...daha fazlasi"]
                models_str = ",\n ".join(models)
                new_note += f" | 📱 {dev_count} Cihaz:\n {models_str}"
            
            new_note = new_note[:1500]
            
            if note != new_note:
                put_resp = await marzban_api_request("PUT", f"/api/user/{urllib.parse.quote(username)}", json_data={"note": new_note})
                if put_resp and put_resp.status_code == 200:
                    print(f"[SYNC] ✅ '{username}' için cihaz bilgileri Marzban notuna eklendi.")
    except Exception as e:
        print(f"[SYNC KRİTİK HATA] ❌ '{username}' senkronize edilirken patladı: {repr(e)}")
# --- MARZBAN API AYARLARI BİTİŞ ---

# --- COK PANELLI ENTEGRASYON (Marzban + 3x-ui) ---
# Paneller global_settings['integrated_panels'] icinde JSON liste olarak tutulur:
# [{id,type:"marzban"|"3xui",name,url,user,pass,enabled,is_main}]
def _panels():
    try:
        p = json.loads(get_global("integrated_panels", "[]"))
        return p if isinstance(p, list) else []
    except: return []

def _host(u):
    try: return (urllib.parse.urlparse(u or "").hostname or "").lower()
    except: return ""

_mph_cache = [0.0, ""]
def _main_panel_host():
    """ANA panel = musteri sub linklerinin geldigi panel (macros'un bagli oldugu panel).
    subs tablosundaki en yaygin host; yoksa .env MARZBAN_URL host'u. 60 sn cache."""
    if time.time() - _mph_cache[0] < 60 and _mph_cache[1]: return _mph_cache[1]
    h = _main_panel_host_calc(); _mph_cache[0], _mph_cache[1] = time.time(), h
    return h

def _main_panel_host_calc():
    from collections import Counter
    try:
        with db_session() as con:
            rows = con.execute("SELECT url FROM subs WHERE url LIKE 'http%'").fetchall()
        c = Counter(_host(r["url"]) for r in rows)
        c.pop("", None)
        if c: return c.most_common(1)[0][0]
    except: pass
    return _host(MARZBAN_URL)

def _effective_panels():
    """Kayitli paneller + (yoksa) .env'deki ana Marzban otomatik. is_main otomatik hesaplanir."""
    ps = [dict(p) for p in _panels()]
    mh = _main_panel_host()
    for p in ps: p["is_main"] = bool(mh) and _host(p.get("url")) == mh
    if MARZBAN_URL and MARZBAN_USER and not any(p["is_main"] for p in ps) and _host(MARZBAN_URL) == mh:
        ps.insert(0, {"type": "marzban", "name": "Ana Panel", "url": MARZBAN_URL, "user": MARZBAN_USER,
                      "pass": MARZBAN_PASS, "enabled": True, "is_main": True, "auto": True})
    ps.sort(key=lambda p: not p.get("is_main"))  # ana panel hep ustte
    return ps

async def _marzban_token(url, user, pwd):
    async with httpx.AsyncClient(verify=False, timeout=12.0) as cl:
        r = await cl.post(f"{url.rstrip('/')}/api/admin/token",
                          data={"grant_type": "password", "username": user, "password": pwd})
        if r.status_code == 200: return r.json().get("access_token")
    return None

async def _marzban_users(url, user, pwd):
    """Marzban /api/users — sayfalamali (buyuk paneller icin). Marzban 0.8.x:
    {total, users:[...]} ve offset/limit parametreleri. Tum kullanicilari toplar."""
    tok = await _marzban_token(url, user, pwd)
    if not tok: return None
    base = url.rstrip("/"); H = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    out, offset, limit = [], 0, 500
    async with httpx.AsyncClient(verify=False, timeout=25.0) as cl:
        while True:
            try:
                r = await cl.get(f"{base}/api/users", headers=H, params={"offset": offset, "limit": limit})
            except: break
            if r.status_code != 200:
                return out if out else None
            d = r.json()
            batch = d.get("users", []) if isinstance(d, dict) else (d or [])
            if not batch: break
            out.extend(batch)
            total = d.get("total") if isinstance(d, dict) else None
            offset += len(batch)
            if (total is not None and offset >= total) or len(batch) < limit: break
    return out

async def _xui_session(url, user, pwd):
    """3x-ui login -> oturum cookie'li httpx client. Modern fork'lar CSRF ister:
    once base'den csrf-token + cookie alinir, sonra X-CSRF-Token ile /login POST edilir."""
    base = url.rstrip("/")
    cl = httpx.AsyncClient(verify=False, timeout=12.0, follow_redirects=False)
    try:
        hdr = {"Referer": base + "/"}
        try:
            r = await cl.get(base + "/")
            m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
            if m: hdr["X-CSRF-Token"] = m.group(1)
        except: pass
        rr = await cl.post(base + "/login", data={"username": user, "password": pwd}, headers=hdr)
        try: ok = rr.json().get("success") is True
        except: ok = False
        if ok: return cl
    except: pass
    await cl.aclose(); return None

def _xui_parse_inbounds(data):
    """Inbound listesinden client'lar. Ayni email birden fazla inbound'da olabilir (ve trafik kaydi
    email basina tektir) — email'e gore TEKILLESTIRILIR, yoksa sayi/trafik inbound sayisi kadar katlanir."""
    raw = _xui_parse_inbounds_raw(data)
    uniq = {}
    for u in raw:
        k = (u.get("username") or "").lower()
        if not k: continue
        if k not in uniq or u["used_traffic"] > uniq[k]["used_traffic"]: uniq[k] = u
    return list(uniq.values())

def _xui_parse_inbounds_raw(data):
    users = []
    for inb in (data.get("obj") or []):
        cstats = inb.get("clientStats") or []
        if cstats:
            for cs in cstats:
                users.append({"username": cs.get("email", ""),
                              "used_traffic": (cs.get("up", 0) or 0) + (cs.get("down", 0) or 0),
                              "status": "active" if cs.get("enable", True) else "disabled"})
        else:
            try:
                st = json.loads(inb.get("settings", "{}") or "{}")
                for c in st.get("clients", []):
                    if c.get("email"): users.append({"username": c["email"], "used_traffic": 0, "status": "active"})
            except: pass
    return users

async def _xui_users(url, token=None, user=None, pwd=None):
    """3x-ui client'lari (email + trafik). Oncelik: Bearer API token (yeni fork'lar,
    panelin Guvenlik ayarlarindan); yoksa user/pass oturum. Endpoint /panel/api/inbounds/list."""
    base = url.rstrip("/")
    if token:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15.0, follow_redirects=True) as cl:
                r = await cl.get(base + "/panel/api/inbounds/list", headers={"Authorization": f"Bearer {token}"})
                j = r.json()
                if isinstance(j, dict) and j.get("success") and j.get("obj") is not None:
                    return _xui_parse_inbounds(j)
        except: pass
        return None
    cl = await _xui_session(url, user, pwd)
    if not cl: return None
    try:
        for method, ep in (("GET", "/panel/api/inbounds/list"), ("POST", "/panel/inbound/list")):
            try:
                r = await cl.request(method, base + ep)
                j = r.json()
                if isinstance(j, dict) and j.get("obj") is not None:
                    return _xui_parse_inbounds(j)
            except: continue
        return None
    finally:
        try: await cl.aclose()
        except: pass

async def _panel_test(ptype, url, user, pwd, token=""):
    """Baglanti testi: (ok, mesaj, kullanici_sayisi)."""
    try:
        if ptype == "marzban":
            us = await _marzban_users(url, user, pwd)
            if us is None: return False, "Giriş/erişim başarısız (Marzban)", 0
            return True, "Bağlandı (Marzban)", len(us)
        elif ptype == "3xui":
            us = await _xui_users(url, token=token or None, user=user, pwd=pwd)
            if us is None: return False, "Erişim başarısız (3x-ui — token/giriş kontrol et)", 0
            return True, "Bağlandı (3x-ui)", len(us)
        return False, "Bilinmeyen panel türü", 0
    except Exception as e:
        return False, f"Hata: {str(e)[:60]}", 0

# ---- YAZMA SENKRONU: ana panel (Marzban) -> ikincil 3x-ui paneller ----
# 3x-ui modern clients API (panelin /panel/api/openapi.json'undan dogrulandi):
#   GET  /panel/api/clients/list            -> email basina tek kayit + inboundIds + traffic{up,down}
#   POST /panel/api/clients/bulkCreate      -> [{client:{...}, inboundIds:[...]}]
#   POST /panel/api/clients/bulkAttach      -> {emails, inboundIds}
#   POST /panel/api/clients/update/{email}  -> tam client (satiri degistirir, kimlik korunur)
#   POST /panel/api/clients/bulkEnable|bulkDisable -> {emails}
#   POST /panel/api/clients/resetTraffic/{email}
#   GET  /panel/api/clients/subLinks/{subId} -> JSON link dizisi (sub birlestirme icin)
_XUI_KEEP = ("email", "subId", "password", "auth", "flow", "security", "limitIp", "limitHwid", "totalGB",
             "expiryTime", "enable", "tgId", "comment", "group", "reset", "trafficReset", "trafficResetDay")
_sync_lock = asyncio.Lock()
_sublinks_cache = {}

def _pkey(p):
    u = urllib.parse.urlparse(p.get("url", "") or "")
    return (u.hostname or "") + (f":{u.port}" if u.port else "") + (u.path or "").rstrip("/")

def _macros_subid(email):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "macros:" + email))

class _XuiConn:
    """Bearer API token ile 3x-ui baglantisi (yazma islemleri token gerektirir)."""
    def __init__(self, p):
        self.base = (p.get("url") or "").rstrip("/")
        self.h = {"Accept": "application/json", "Authorization": f"Bearer {p.get('token') or ''}"}
        self.cl = None
    async def __aenter__(self):
        self.cl = httpx.AsyncClient(verify=False, timeout=90.0, follow_redirects=True); return self
    async def __aexit__(self, *a):
        try: await self.cl.aclose()
        except: pass
    async def call(self, method, ep, **kw):
        r = await self.cl.request(method, self.base + ep, headers=self.h, **kw)
        try: return r.json()
        except: return {"success": False, "msg": f"HTTP {r.status_code}"}

async def _xui_clients(conn):
    j = await conn.call("GET", "/panel/api/clients/list")
    return j["obj"] if j.get("success") and isinstance(j.get("obj"), list) else None

async def _panel_fetch_users(p):
    """Durum/test icin: kullanici adi + trafik listesi."""
    if p.get("type") == "marzban":
        us = await _marzban_users(p.get("url", ""), p.get("user", ""), p.get("pass", ""))
        if us is None: return None
        return [{"username": (u.get("username") or "").strip(), "used": int(u.get("used_traffic") or 0)} for u in us]
    elif p.get("type") == "3xui":
        us = await _xui_users(p.get("url", ""), token=p.get("token") or None, user=p.get("user"), pwd=p.get("pass"))
        if us is None: return None
        return [{"username": (u.get("username") or "").strip(), "used": int(u.get("used_traffic") or 0)} for u in us]
    return None

_create_fail = {}  # (pkey,email) -> (ts, gerekce): basarisiz olusturma tekrar denenmeden once 15 dk bekler

def _xui_email_map(pk, main_names, cur):
    """Ana panel kullanici adi -> 3x-ui email. 3x-ui email'i harf-duyarsiz benzersizdir; ana panelde
    'Kako'/'kako' gibi ikizler varsa biri sabit ekle acilir. Eslemeler panel_links'te kalicidir."""
    stored = {}
    try:
        with db_session() as con:
            for r in con.execute("SELECT username,email FROM panel_links WHERE pkey=?", (pk,)).fetchall():
                if r["email"]: stored[r["username"]] = r["email"]
    except Exception: pass
    ci = {e.lower(): e for e in cur}
    groups = {}
    for un in main_names: groups.setdefault(un.lower(), []).append(un)
    out = {}
    for un in main_names:
        if un in stored: out[un] = stored[un]; continue
        if un in cur: out[un] = un; continue
        grp = sorted(groups[un.lower()])
        owner = next((g for g in grp if g in cur), grp[0])
        if un == owner:
            out[un] = ci.get(un.lower(), un)  # farkli harfle var olan client'i sahiplen
        else:
            out[un] = f"{un}-{hashlib.sha1(un.encode()).hexdigest()[:4]}"
    return out

async def _mirror_to_xui(p, main_users, clients, combined, safe_disable, reset_users):
    st = {"created": 0, "attached": 0, "updated": 0, "enabled": 0, "disabled": 0, "reset": 0, "skipped": [], "errors": []}
    cur = {c.get("email"): c for c in clients if c.get("email")}
    _own = lambda c: c.get("group") == "macros" and c.get("subId") == _macros_subid(c.get("email") or "")
    st["macros_before"] = sum(1 for c in cur.values() if _own(c))   # macros'un daha once actiklari
    st["own"] = len(cur) - st["macros_before"]                      # panelin kendi (onceden var olan) kullanicilari
    pk = _pkey(p)
    mu = {(u.get("username") or "").strip(): u for u in main_users if (u.get("username") or "").strip()}
    emap = _xui_email_map(pk, list(mu.keys()), cur)
    now = time.time()
    async with _XuiConn(p) as conn:
        ij = await conn.call("GET", "/panel/api/inbounds/list")
        all_ids = sorted(i["id"] for i in (ij.get("obj") or []) if i.get("id") is not None)
        if not all_ids:
            st["errors"].append("inbound bulunamadı"); return st
        st["shared"] = sum(1 for u in mu if (cur.get(emap[u]) is not None and not _own(cur[emap[u]])))  # ana panel kullanicisi, panelde zaten vardi
        creates, attach, updates, en, dis, links = [], {}, [], [], [], {}
        for un, u in mu.items():
            em = emap[un]
            limit = int(u.get("data_limit") or 0)
            exp = int(u.get("expire") or 0) * 1000
            active = (u.get("status") in ("active", "on_hold"))
            over = limit > 0 and combined.get(un, 0) >= limit
            want = active and not over
            c = cur.get(em)
            if c is None:
                f = _create_fail.get((pk, em))
                if f and now - f[0] < 900:
                    st["skipped"].append({"email": em, "reason": f[1], "retry_in_s": int(900 - (now - f[0]))}); continue
                sid = _macros_subid(em)
                creates.append({"client": {"email": em, "subId": sid, "totalGB": limit, "expiryTime": exp, "limitIp": 0,
                                           "enable": want, "group": "macros", "comment": f"macros-sync:{un}"},
                                "inboundIds": all_ids})
                links[un] = (em, sid)
                continue
            links[un] = (em, c.get("subId") or "")
            miss = [i for i in all_ids if i not in (c.get("inboundIds") or [])]
            if miss: attach.setdefault(tuple(miss), []).append(em)
            if int(c.get("totalGB") or 0) != limit or int(c.get("expiryTime") or 0) != exp:
                pl = {k: c.get(k) for k in _XUI_KEEP}; pl["id"] = c.get("uuid")
                pl["totalGB"], pl["expiryTime"], pl["enable"] = limit, exp, want
                updates.append((em, pl))
            elif bool(c.get("enable")) != want:
                (en if want else dis).append(em)
        managed = {emap[un] for un in mu}
        if safe_disable:  # ana panelden kaldirilan (macros'un actigi) client'lar SILINMEZ, kapatilir
            for em, c in cur.items():
                if em not in managed and c.get("group") == "macros" and c.get("enable"):
                    dis.append(em)
        for un in sorted(reset_users):
            em = emap.get(un)
            if em and em in cur:
                j = await conn.call("POST", f"/panel/api/clients/resetTraffic/{urllib.parse.quote(em, safe='')}")
                if j.get("success"): st["reset"] += 1
                else: st["errors"].append(f"reset {em}: {j.get('msg')}")
        async def _one(item):
            j = await conn.call("POST", "/panel/api/clients/add", json=item)
            em = item["client"]["email"]
            if j.get("success"): st["created"] += 1; _create_fail.pop((pk, em), None)
            else:
                _create_fail[(pk, em)] = (time.time(), str(j.get("msg"))); st["skipped"].append({"email": em, "reason": str(j.get("msg"))})
        for i in range(0, len(creates), 25):
            batch = creates[i:i + 25]
            try:
                j = await conn.call("POST", "/panel/api/clients/bulkCreate", json=batch)
            except Exception as e:
                j = {"success": False, "msg": repr(e)}
            if not j.get("success"):
                st["errors"].append(f"bulkCreate partisi başarısız ({j.get('msg')}), tek tek deneniyor")
                for item in batch: await _one(item)
                continue
            obj = j.get("obj") if isinstance(j.get("obj"), dict) else {}
            sk = obj.get("skipped") or []
            sk = sk if isinstance(sk, list) else [{"email": k, "reason": v} for k, v in dict(sk).items()]
            skipped_emails = set()
            for x in sk:
                em = (x.get("email") if isinstance(x, dict) else str(x)) or ""
                rs = (x.get("reason") if isinstance(x, dict) else "") or ""
                skipped_emails.add(em); _create_fail[(pk, em)] = (time.time(), rs)
                st["skipped"].append({"email": em, "reason": rs})
            st["created"] += len(batch) - len(skipped_emails)
            for item in batch:
                if item["client"]["email"] not in skipped_emails: _create_fail.pop((pk, item["client"]["email"]), None)
        for miss, ems in attach.items():
            j = await conn.call("POST", "/panel/api/clients/bulkAttach", json={"emails": ems, "inboundIds": list(miss)})
            if j.get("success"): st["attached"] += len(ems)
            else: st["errors"].append(f"bulkAttach: {j.get('msg')}")
        for em, pl in updates:
            j = await conn.call("POST", f"/panel/api/clients/update/{urllib.parse.quote(em, safe='')}", json=pl)
            if j.get("success"): st["updated"] += 1
            else: st["errors"].append(f"update {em}: {j.get('msg')}")
        if en:
            j = await conn.call("POST", "/panel/api/clients/bulkEnable", json={"emails": en})
            if j.get("success"): st["enabled"] += len(en)
            else: st["errors"].append(f"bulkEnable: {j.get('msg')}")
        if dis:
            j = await conn.call("POST", "/panel/api/clients/bulkDisable", json={"emails": sorted(set(dis))})
            if j.get("success"): st["disabled"] += len(set(dis))
            else: st["errors"].append(f"bulkDisable: {j.get('msg')}")
    with DB_LOCK, db_session() as con:
        for un, (em, sid) in links.items():
            if sid and (em in cur or not any(x.get("email") == em for x in st["skipped"])):
                con.execute("INSERT INTO panel_links(pkey,username,sub_id,email,updated) VALUES(?,?,?,?,datetime('now')) "
                            "ON CONFLICT(pkey,username) DO UPDATE SET sub_id=?,email=?,updated=datetime('now')", (pk, un, sid, em, sid, em))
        con.commit()
    if st["skipped"]:
        add_panel_log("PANEL_MIRROR_SKIP", json.dumps(st["skipped"], ensure_ascii=False)[:4000], "")
    return st

async def _sync_panels():
    if _sync_lock.locked():
        return {"ok": False, "msg": "Senkron zaten çalışıyor, birazdan tekrar dene"}
    async with _sync_lock:
        return await _sync_panels_inner()

async def _sync_panels_inner():
    """Ana panel -> ikincil paneller senkronu. Trafik = tum panellerin toplami.
    GUVENLIK: hicbir client SILINMEZ; ana panel okunamazsa ya da kullanici sayisi anormal
    duserse kapatma yapilmaz; hata/kismi basari panel_logs'a yazilir."""
    panels = [p for p in _effective_panels() if p.get("enabled", True)]
    main = next((p for p in panels if p.get("is_main")), None)
    secs = [p for p in panels if not p.get("is_main")]
    failures, report, usage = [], {}, {}
    mname = (main.get("name") if main else None) or "Ana Panel"
    main_users = None
    if main and main.get("type") == "marzban":
        try: main_users = await _marzban_users(main["url"], main.get("user", ""), main.get("pass", ""))
        except Exception: main_users = None
        if main_users is None: failures.append(mname)
        else:
            for u in main_users:
                un = (u.get("username") or "").strip()
                if un: usage.setdefault(un, {})[mname] = int(u.get("used_traffic") or 0)
    # ana panelde trafigi sifirlanan kullanicilar (onceki senkrona gore azalmis)
    prev = {}
    try:
        with db_session() as con:
            for r in con.execute("SELECT username,detail FROM panel_users").fetchall():
                try: prev[r["username"]] = json.loads(r["detail"] or "{}")
                except: pass
    except: pass
    reset_users = set()
    if main_users is not None:
        for un, d in usage.items():
            old = (prev.get(un) or {}).get(mname)
            if old is not None and d.get(mname, 0) + 1048576 < int(old):
                reset_users.add(un)
    sec_clients = {}
    for p in secs:
        name = p.get("name") or _host(p.get("url")) or p.get("type")
        try:
            if p.get("type") == "3xui":
                clients = None
                if p.get("token"):
                    async with _XuiConn(p) as conn:
                        clients = await _xui_clients(conn)
                if clients is not None:
                    sec_clients[_pkey(p)] = clients
                    e2u = {}
                    try:
                        with db_session() as con:
                            for r in con.execute("SELECT username,email FROM panel_links WHERE pkey=?", (_pkey(p),)).fetchall():
                                if r["email"]: e2u[r["email"]] = r["username"]
                    except Exception: pass
                    for c in clients:
                        t = c.get("traffic") or {}
                        em = c.get("email")
                        if not em: continue
                        un = e2u.get(em, em)
                        usage.setdefault(un, {})[name] = 0 if un in reset_users else int(t.get("up") or 0) + int(t.get("down") or 0)
                else:
                    us = await _xui_users(p.get("url", ""), token=p.get("token") or None, user=p.get("user"), pwd=p.get("pass"))
                    if us is None: raise RuntimeError("okunamadı")
                    seen = {}
                    for u in us:
                        if u["username"]: seen[u["username"]] = max(seen.get(u["username"], 0), int(u["used_traffic"] or 0))
                    for em, v in seen.items(): usage.setdefault(em, {})[name] = v
            elif p.get("type") == "marzban":
                us = await _marzban_users(p.get("url", ""), p.get("user", ""), p.get("pass", ""))
                if us is None: raise RuntimeError("okunamadı")
                for u in us:
                    un = (u.get("username") or "").strip()
                    if un: usage.setdefault(un, {})[name] = int(u.get("used_traffic") or 0)
        except Exception as e:
            failures.append(name); print(f"[PANEL_SYNC] {name}: {e!r}")
    if main_users is None and not usage:
        add_panel_log("PANEL_SYNC_FAIL", f"Hiçbir panele bağlanılamadı: {', '.join(failures) or '-'}", "")
        return {"ok": False, "msg": "Hiçbir panele bağlanılamadı — veri korundu", "failures": failures, "users": 0}
    combined = {un: sum(d.values()) for un, d in usage.items()}
    with DB_LOCK, db_session() as con:
        for un, d in usage.items():
            pset, det = ",".join(sorted(d.keys())), json.dumps(d)
            con.execute("""INSERT INTO panel_users(username,total_used,panels,detail,last_sync) VALUES(?,?,?,?,datetime('now'))
                           ON CONFLICT(username) DO UPDATE SET total_used=?,panels=?,detail=?,last_sync=datetime('now')""",
                        (un, combined[un], pset, det, combined[un], pset, det))
        con.commit()
    if main_users is not None:
        prev_n = int(get_global("panel_main_count", "0") or 0); n = len(main_users)
        safe_disable = n > 0 and not (prev_n > 20 and n < prev_n * 0.5)
        if not safe_disable:
            add_panel_log("PANEL_SYNC_GUARD", f"Ana panel kullanıcı sayısı anormal ({prev_n}->{n}); kapatma atlandı", "")
        set_global("panel_main_count", str(n))
        for p in secs:
            if p.get("type") != "3xui" or p.get("mirror", True) is False: continue
            nm = p.get("name") or _host(p.get("url"))
            if not p.get("token"):
                report[nm] = {"errors": ["yazma için 3x-ui API Token gerekli"]}; continue
            clients = sec_clients.get(_pkey(p))
            if clients is None: continue
            try: report[nm] = await _mirror_to_xui(p, main_users, clients, combined, safe_disable, reset_users)
            except Exception as e:
                failures.append(nm); report[nm] = {"errors": [repr(e)[:200]]}
    if failures:
        add_panel_log("PANEL_SYNC_PARTIAL", f"Başarısız: {', '.join(failures)}", "")
    for nm, r in report.items():
        if any(r.get(k) for k in ("created", "attached", "updated", "enabled", "disabled", "reset")) or r.get("errors"):
            add_panel_log("PANEL_MIRROR", f"{nm}: " + json.dumps({k: v for k, v in r.items() if v}, ensure_ascii=False)[:400], "")
    return {"ok": True, "users": len(combined), "panels": len(panels) - len(failures), "failures": failures,
            "mirror": report, "main_users": len(main_users) if main_users is not None else None}

def _sub_username(sub):
    """Musterinin ana paneldeki kullanici adi: Marzban sub token'i = base64("kullanici,zaman")+imza.
    Cozulemezse sub adina duser."""
    url = (sub["url"] if sub else "") or ""
    try:
        m = re.search(r"/sub/([A-Za-z0-9_\-=]+)", urllib.parse.urlparse(url).path)
        if m:
            tok = m.group(1)
            for cut in range(len(tok), 7, -1):
                t = tok[:cut]
                if len(t) % 4 == 1: continue
                try: raw = base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))
                except Exception: continue
                mm = re.match(rb"^([^,\x00-\x1f]{1,64}),(\d{9,11})", raw)
                if mm: return mm.group(1).decode("utf-8", "ignore")
    except Exception: pass
    return (sub["name"] if sub else "") or ""

async def _secondary_links(username):
    """Bir kullanicinin ikincil 3x-ui panellerdeki config linkleri (duz liste)."""
    out = []
    for links in (await _secondary_links_by_panel(username)).values(): out += links
    return out

async def _secondary_links_by_panel(username):
    """Ikincil 3x-ui panellerdeki config linkleri, panel anahtarina (pkey) gore. 30 sn cache."""
    if not username: return {}
    res = {}
    for p in _effective_panels():
        if not p.get("enabled", True) or p.get("is_main") or p.get("type") != "3xui" or p.get("merge", True) is False or not p.get("token"):
            continue
        pk = _pkey(p)
        out = res.setdefault(pk, [])
        try:
            with db_session() as con:
                r = con.execute("SELECT sub_id FROM panel_links WHERE pkey=? AND username=?", (pk, username)).fetchone()
            sid = r["sub_id"] if r else None
        except Exception: sid = None
        if not sid: continue
        ck, now = (pk, sid), time.time()
        hit = _sublinks_cache.get(ck)
        if hit and now - hit[0] < 30:
            out += hit[1]; continue
        try:
            async with _XuiConn(p) as conn:
                j = await conn.call("GET", f"/panel/api/clients/subLinks/{urllib.parse.quote(sid, safe='')}")
            links = [l for l in (j.get("obj") or []) if isinstance(l, str) and "://" in l] if j.get("success") else []
            _sublinks_cache[ck] = (now, links); out += links
        except Exception:
            if hit: out += hit[1]
    return res

def _panel_user_total(username):
    """Bir kullanicinin tum panellerdeki toplam trafigi (yoksa None)."""
    if not username: return None
    try:
        with db_session() as con:
            r = con.execute("SELECT total_used FROM panel_users WHERE username=?", (username,)).fetchone()
        return int(r["total_used"]) if r else None
    except: return None
# --- COK PANELLI ENTEGRASYON BİTİŞ ---

@contextmanager
def db_session():
    con = sqlite3.connect(DB, timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    try: yield con
    finally: con.close()

DEVICE_ID_HEADERS = ["x-hwid","x-device-id","x-sub-token","x-client-id","x-unique-id","x-device-token","x-hardware-id","x-fingerprint"]
DEVICE_MODEL_HEADERS = ["x-device-model","x-model","x-device-name","x-phone-model","x-device-type","x-client-name","x-device-info"]
PROTOCOLS = ["ss", "vless", "vmess", "trojan", "hysteria2", "hy2", "tuic"]
VLESS_SUBTYPES = ["reality", "grpc", "xhttp", "ws", "tcp", "other"]
VMESS_SUBTYPES = ["ws", "grpc", "tcp", "other"]

FLAG_MAP = {
    "🇹🇷": "Turkey - Ankara", "🇩🇪": "Germany - Berlin", "🇺🇸": "USA - Washington", "🇬🇧": "UK - London",
    "🇳🇱": "Netherlands - Amsterdam", "🇫🇷": "France - Paris", "🇷🇺": "Russia - Moscow", "🇺🇦": "Ukraine - Kyiv",
    "🇮🇹": "Italy - Rome", "🇪🇸": "Spain - Madrid", "🇨🇦": "Canada - Ottawa", "🇨🇭": "Switzerland - Bern",
    "🇸🇪": "Sweden - Stockholm", "🇵🇱": "Poland - Warsaw", "🇦🇹": "Austria - Vienna", "🇧🇬": "Bulgaria - Sofia"
}

def _col_exists(con, table, col):
    try: return any(r["name"] == col for r in con.execute(f"PRAGMA table_info({table})").fetchall())
    except: return False

def init_db():
    with DB_LOCK, db_session() as con:
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS subs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', url TEXT NOT NULL UNIQUE, device_limit INTEGER DEFAULT 1, blocked INTEGER DEFAULT 0, metadata TEXT DEFAULT '', blocked_metadata TEXT DEFAULT '', blocked_device_metadata TEXT DEFAULT '', note TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')), last_checked TEXT DEFAULT '', last_check_status TEXT DEFAULT '', last_check_username TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER NOT NULL, hardware_id TEXT NOT NULL, app_name TEXT DEFAULT '', device_model TEXT DEFAULT '', first_seen TEXT DEFAULT (datetime('now')), last_seen TEXT DEFAULT (datetime('now')), ip TEXT DEFAULT '', user_agent TEXT DEFAULT '', blocked INTEGER DEFAULT 0, UNIQUE(sub_id, hardware_id));
            CREATE TABLE IF NOT EXISTS ua_blacklist (id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS ua_whitelist (id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER, hardware_id TEXT DEFAULT '', ip TEXT DEFAULT '', user_agent TEXT DEFAULT '', event TEXT NOT NULL, detail TEXT DEFAULT '', ts TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS traffic (id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER NOT NULL, bytes_down INTEGER DEFAULT 0, req_count INTEGER DEFAULT 0, last_update TEXT DEFAULT (datetime('now')), UNIQUE(sub_id));
            CREATE TABLE IF NOT EXISTS panel_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, detail TEXT DEFAULT '', ip TEXT DEFAULT '', ts TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS panel_sessions (sid TEXT PRIMARY KEY, username TEXT, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS panel_links (pkey TEXT, username TEXT, sub_id TEXT, updated TEXT DEFAULT (datetime('now')), PRIMARY KEY(pkey, username));
            CREATE TABLE IF NOT EXISTS panel_users (username TEXT PRIMARY KEY, total_used INTEGER DEFAULT 0, panels TEXT DEFAULT '', detail TEXT DEFAULT '', last_sync TEXT DEFAULT (datetime('now')));
        """)
        if not _col_exists(con, "subs", "unlimited"): con.execute("ALTER TABLE subs ADD COLUMN unlimited INTEGER DEFAULT 0")
        if not _col_exists(con, "subs", "device_limit_enabled"): con.execute("ALTER TABLE subs ADD COLUMN device_limit_enabled INTEGER DEFAULT 1")
        if not _col_exists(con, "subs", "force_limit"): con.execute("ALTER TABLE subs ADD COLUMN force_limit INTEGER DEFAULT 0")
        try:
            if not _col_exists(con, "panel_links", "email"): con.execute("ALTER TABLE panel_links ADD COLUMN email TEXT DEFAULT ''")
        except Exception: pass
        if not _col_exists(con, "subs", "token"):
            con.execute("ALTER TABLE subs ADD COLUMN token TEXT DEFAULT ''")
        # Token'i olmayan mevcut sub'lara benzersiz token uret (kisa-yol /macros/<token> icin)
        for r in con.execute("SELECT id FROM subs WHERE token IS NULL OR token=''").fetchall():
            con.execute("UPDATE subs SET token=? WHERE id=?", (secrets.token_urlsafe(16), r["id"]))
        con.execute("CREATE INDEX IF NOT EXISTS idx_subs_token ON subs(token)")
        con.execute("INSERT INTO global_settings(key,value) VALUES('device_limit_enabled','1') ON CONFLICT(key) DO NOTHING")
        con.commit()

_global_cache = {}
_global_cache_lock = threading.Lock()

def get_global(key, default=""):
    # Yalnizca DB'de gercekten var olan degerler cache'lenir; eksik anahtarlarda
    # her cagri kendi default'unu dondurur. Yazma set_global uzerinden oldugu icin
    # cache her zaman tutarli kalir.
    with _global_cache_lock:
        if key in _global_cache: return _global_cache[key]
    row = None
    with db_session() as con:
        try: row = con.execute("SELECT value FROM global_settings WHERE key=?",(key,)).fetchone()
        except Exception as e: print(f"[DB] get_global('{key}') hatasi: {e!r}")
    if row is not None:
        with _global_cache_lock: _global_cache[key] = row["value"]
        return row["value"]
    return default

def set_global(key, value):
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO global_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",(key,value,value)); con.commit()
        except Exception as e: print(f"[DB] set_global('{key}') hatasi: {e!r}")
    with _global_cache_lock: _global_cache[key] = value

def get_sys_info():
    cpu = "0.00"
    try: cpu = f"{os.getloadavg()[0]:.2f}"
    except: pass
    ram_pct, disk_pct = 0, 0
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
            tot = free_mem = buffers = cached = 0
            for l in lines:
                if l.startswith("MemTotal:"): tot = int(l.split()[1])
                elif l.startswith("MemFree:"): free_mem = int(l.split()[1])
                elif l.startswith("Buffers:"): buffers = int(l.split()[1])
                elif l.startswith("Cached:"): cached = int(l.split()[1])
            if tot > 0: ram_pct = round((tot - free_mem - buffers - cached) / tot * 100, 1)
        total, used, free = shutil.disk_usage("/")
        disk_pct = round(used / total * 100, 1)
    except: pass
    return {"cpu": cpu, "ram": ram_pct, "disk": disk_pct}

def parse_link_for_xray(link):
    try:
        if link.startswith("vmess://"):
            d = json.loads(base64.b64decode(link[8:] + "===").decode('utf-8'))
            out = {"protocol": "vmess", "settings": {"vnext": [{"address": d.get("add",""), "port": int(d.get("port",443)), "users": [{"id": d.get("id",""), "alterId": int(d.get("aid", 0)), "security": d.get("scy", "auto")}]}]}}
            net, tls = d.get("net", "tcp"), d.get("tls", "none")
            stream = {"network": net, "security": "tls" if tls == "tls" else "none"}
            if net == "ws":
                ws_host = d.get("host", d.get("sni", d.get("add", "")))
                stream["wsSettings"] = {"path": d.get("path", "/"), "headers": {"Host": ws_host} if ws_host else {}}
            if tls == "tls": stream["tlsSettings"] = {"serverName": d.get("sni", d.get("host", d.get("add", ""))), "allowInsecure": True}
            out["streamSettings"] = stream
            return out
        elif link.startswith("vless://") or link.startswith("trojan://"):
            proto = "vless" if link.startswith("vless://") else "trojan"
            parsed = urllib.parse.urlparse(link)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            addr, port, pw_id = parsed.hostname or "", parsed.port or 443, parsed.username or ""
            out = {"protocol": proto, "settings": {}}
            if proto == "vless":
                user_dict = {"id": pw_id, "encryption": qs.get("encryption", "none")}
                if qs.get("flow"): user_dict["flow"] = qs.get("flow")
                out["settings"]["vnext"] = [{"address": addr, "port": port, "users": [user_dict]}]
            else:
                out["settings"]["servers"] = [{"address": addr, "port": port, "password": pw_id}]
            
            net, security = qs.get("type", "tcp"), qs.get("security", "none")
            stream = {"network": net, "security": security}
            
            # VLESS WS KORUMASI: Host başlığı yoksa SNI'yi veya ADDR'yi kullan (Cloudflare bloklarına karşı)
            if net == "ws":
                ws_host = qs.get("host", qs.get("sni", addr))
                stream["wsSettings"] = {"path": qs.get("path", "/"), "headers": {"Host": ws_host} if ws_host else {}}
            elif net == "grpc": stream["grpcSettings"] = {"serviceName": qs.get("serviceName", ""), "multiMode": qs.get("mode", "multi") == "multi"}
            elif net == "xhttp": stream["xhttpSettings"] = {"path": qs.get("path", "/"), "host": qs.get("host", ""), "mode": qs.get("mode", "auto")}

            if security == "tls": stream["tlsSettings"] = {"serverName": qs.get("sni", qs.get("host", addr)), "allowInsecure": True, "fingerprint": qs.get("fp", "chrome")}
            elif security == "reality": stream["realitySettings"] = {"serverName": qs.get("sni", addr), "publicKey": qs.get("pbk", ""), "shortId": qs.get("sid", ""), "fingerprint": qs.get("fp", "chrome"), "spiderX": qs.get("spx", "/")}
            out["streamSettings"] = stream
            return out
        elif link.startswith("ss://"):
            s = link[5:].split("#")[0]
            if "@" in s:
                mp, hp = s.split("@")
                try: mp = base64.b64decode(mp + "===").decode('utf-8')
                except: pass
                method, pwd = mp.split(":", 1)
                host, port = hp.split(":")
            else:
                dec = base64.b64decode(s + "===").decode('utf-8')
                mp, hp = dec.split("@")
                method, pwd = mp.split(":", 1)
                host, port = hp.split(":")
            return {"protocol": "shadowsocks", "settings": {"servers": [{"address": host, "port": int(port), "method": method, "password": pwd}]}}
    except: pass
    return None

def get_outb_target(outb):
    try:
        if "vnext" in outb.get("settings", {}):
            return outb["settings"]["vnext"][0]["address"], outb["settings"]["vnext"][0]["port"]
        elif "servers" in outb.get("settings", {}):
            return outb["settings"]["servers"][0]["address"], outb["settings"]["servers"][0]["port"]
    except: pass
    return None, None
# -----------------------------------------------

# --- CDN IP TESPİTİ (VLESS WS node'larinin IP'si hangi CDN'e ait?) ---
_cdn_ranges = {"google": [], "cloudfront": [], "fastly": []}
_cdn_ranges_ts = 0
_cdn_host_cache = {}          # host -> provider adi veya None
_cdn_lock = asyncio.Lock()

async def _load_cdn_ranges():
    """CDN IP araliklarini resmi kaynaklardan ceker (gunde bir yeniler)."""
    global _cdn_ranges_ts
    if _cdn_ranges["google"] and (time.time() - _cdn_ranges_ts) < 86400: return
    async with _cdn_lock:
        if _cdn_ranges["google"] and (time.time() - _cdn_ranges_ts) < 86400: return
        g, c, fa = [], [], []
        async with httpx.AsyncClient(timeout=10, verify=False) as cl:
            try:
                r = await cl.get("https://www.gstatic.com/ipranges/goog.json")
                for p in r.json().get("prefixes", []):
                    v = p.get("ipv4Prefix") or p.get("ipv6Prefix")
                    if v: g.append(ipaddress.ip_network(v, strict=False))
            except Exception as e: print(f"[CDN] google yuklenemedi: {e!r}")
            try:
                r = await cl.get("https://ip-ranges.amazonaws.com/ip-ranges.json")
                data = r.json()
                for p in data.get("prefixes", []):
                    if p.get("service") == "CLOUDFRONT": c.append(ipaddress.ip_network(p["ip_prefix"], strict=False))
                for p in data.get("ipv6_prefixes", []):
                    if p.get("service") == "CLOUDFRONT": c.append(ipaddress.ip_network(p["ipv6_prefix"], strict=False))
            except Exception as e: print(f"[CDN] cloudfront yuklenemedi: {e!r}")
            try:
                r = await cl.get("https://api.fastly.com/public-ip-list")
                j = r.json()
                for v in (j.get("addresses", []) + j.get("ipv6_addresses", [])):
                    fa.append(ipaddress.ip_network(v, strict=False))
            except Exception as e: print(f"[CDN] fastly yuklenemedi: {e!r}")
        if g or c or fa:
            _cdn_ranges["google"], _cdn_ranges["cloudfront"], _cdn_ranges["fastly"] = g, c, fa
            _cdn_ranges_ts = time.time()
            print(f"[CDN] Araliklar yuklendi: google={len(g)} cloudfront={len(c)} fastly={len(fa)}")

def _classify_ip(ipstr):
    try: ip = ipaddress.ip_address(ipstr)
    except: return None
    # Spesifik CDN'ler once; google en genis oldugu icin sonda
    for name in ("cloudfront", "fastly", "google"):
        for net in _cdn_ranges.get(name, []):
            if ip.version == net.version and ip in net: return name
    return None

async def classify_host(host):
    """Bir host/IP'nin hangi CDN'e ait oldugunu dondurur (google/cloudfront/fastly) ya da None."""
    host = (host or "").strip().strip("[]")
    if not host: return None
    if host in _cdn_host_cache: return _cdn_host_cache[host]
    await _load_cdn_ranges()
    ips = []
    try:
        ipaddress.ip_address(host); ips = [host]
    except ValueError:
        try:
            infos = await asyncio.get_event_loop().getaddrinfo(host, None)
            ips = list({i[4][0] for i in infos})
        except: ips = []
    prov = None
    for ipx in ips:
        prov = _classify_ip(ipx)
        if prov: break
    _cdn_host_cache[host] = prov
    return prov

def _node_host(line):
    """Node'un baglandigi host/adres (vmess icin base64 json 'add', digerleri urlparse)."""
    try:
        if line.startswith("vmess://"):
            d = json.loads(base64.b64decode(line[len("vmess://"):] + "==").decode("utf-8"))
            return (d.get("add", "") or "").strip()
        return (urllib.parse.urlparse(line).hostname or "").strip()
    except: return ""

def _cdn_label_map():
    base = {"google": "GOOGLE", "cloudfront": "CLOUDFRONT", "fastly": "FASTLY"}
    try: m = json.loads(get_global("cdn_labels", "") or "{}")
    except: m = {}
    if isinstance(m, dict): base.update({k: v for k, v in m.items() if isinstance(v, str)})
    return base

async def _cdn_label_for(line, cdn_on):
    """Sadece VLESS WS node'lari icin CDN etiketi; etiket metni kullanici tarafindan ayarlanabilir."""
    if not cdn_on: return ""
    low = line.lower()
    if not ("type=ws" in low or "net=ws" in low): return ""
    prov = await classify_host(_node_host(line))
    return _cdn_label_map().get(prov, "") if prov else ""
# --- CDN IP TESPİTİ BİTİŞ ---

# --- CLOUD/VPS IP TESPİTİ (ws/xhttp disindaki dogrudan node'lar: Azure/AWS/Google/Oracle) ---
_vps_ranges = {"azure": [], "amazon": [], "google": [], "oracle": []}
_vps_ranges_ts = 0
_vps_host_cache = {}
_vps_lock = asyncio.Lock()
_VPS_LABELS = {"azure": "AZURE", "amazon": "AWS", "google": "GOOGLE", "oracle": "ORACLE"}

async def _load_vps_ranges():
    global _vps_ranges_ts
    if _vps_ranges["amazon"] and (time.time() - _vps_ranges_ts) < 86400: return
    async with _vps_lock:
        if _vps_ranges["amazon"] and (time.time() - _vps_ranges_ts) < 86400: return
        az, am, go, orc = [], [], [], []
        async with httpx.AsyncClient(timeout=12, verify=False, follow_redirects=True) as cl:
            try:
                r = await cl.get("https://ip-ranges.amazonaws.com/ip-ranges.json")
                for p in r.json().get("prefixes", []):
                    if p.get("service") == "EC2": am.append(ipaddress.ip_network(p["ip_prefix"], strict=False))
            except Exception as e: print(f"[VPS] aws: {e!r}")
            try:
                r = await cl.get("https://www.gstatic.com/ipranges/cloud.json")
                for p in r.json().get("prefixes", []):
                    v = p.get("ipv4Prefix") or p.get("ipv6Prefix")
                    if v: go.append(ipaddress.ip_network(v, strict=False))
            except Exception as e: print(f"[VPS] gcloud: {e!r}")
            try:
                r = await cl.get("https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json")
                for region in r.json().get("regions", []):
                    for c in region.get("cidrs", []):
                        if c.get("cidr"): orc.append(ipaddress.ip_network(c["cidr"], strict=False))
            except Exception as e: print(f"[VPS] oracle: {e!r}")
            try:
                pg = await cl.get("https://www.microsoft.com/en-us/download/details.aspx?id=56519")
                mt = re.search(r'https://download\.microsoft\.com/download/[^"\']+?ServiceTags_Public_\d+\.json', pg.text)
                if mt:
                    rj = await cl.get(mt.group(0))
                    for v in rj.json().get("values", []):
                        for pfx in v.get("properties", {}).get("addressPrefixes", []):
                            try: az.append(ipaddress.ip_network(pfx, strict=False))
                            except: pass
            except Exception as e: print(f"[VPS] azure: {e!r}")
        if am or go or orc or az:
            _vps_ranges["amazon"], _vps_ranges["google"], _vps_ranges["oracle"], _vps_ranges["azure"] = am, go, orc, az
            _vps_ranges_ts = time.time()
            print(f"[VPS] Araliklar: azure={len(az)} amazon={len(am)} google={len(go)} oracle={len(orc)}")

def _classify_vps_ip(ipstr):
    try: ip = ipaddress.ip_address(ipstr)
    except: return None
    for name in ("azure", "oracle", "google", "amazon"):
        for net in _vps_ranges.get(name, []):
            if ip.version == net.version and ip in net: return name
    return None

async def classify_vps_host(host):
    host = (host or "").strip().strip("[]")
    if not host: return None
    if host in _vps_host_cache: return _vps_host_cache[host]
    await _load_vps_ranges()
    ips = []
    try:
        ipaddress.ip_address(host); ips = [host]
    except ValueError:
        try:
            infos = await asyncio.get_event_loop().getaddrinfo(host, None)
            ips = list({i[4][0] for i in infos})
        except: ips = []
    prov = None
    for ipx in ips:
        prov = _classify_vps_ip(ipx)
        if prov: break
    _vps_host_cache[host] = prov
    return prov

def _vps_enabled_providers():
    if get_global("vps_label_enabled", "0") != "1": return set()
    raw = get_global("vps_providers", "azure,amazon,google,oracle")
    return {p.strip() for p in raw.split(",") if p.strip()}

def _vps_label_map():
    base = dict(_VPS_LABELS)
    try: m = json.loads(get_global("vps_labels", "") or "{}")
    except: m = {}
    if isinstance(m, dict): base.update({k: v for k, v in m.items() if isinstance(v, str)})
    return base

async def _vps_label_for(line, providers):
    """ws/xhttp disindaki dogrudan node'lar icin cloud saglayici etiketi (metin kullanici ayarli)."""
    if not providers: return ""
    low = line.lower()
    if "type=ws" in low or "net=ws" in low or "type=xhttp" in low: return ""
    prov = await classify_vps_host(_node_host(line))
    return _vps_label_map().get(prov, "") if (prov in providers) else ""
# --- CLOUD/VPS IP TESPİTİ BİTİŞ ---

# --- ISTEMCI PROFILLERI (app'e gore cikti formati; User-Agent ile eslesir) ---
def _client_profiles():
    try: p = json.loads(get_global("client_profiles", "[]"))
    except: p = []
    return p if isinstance(p, list) else []

def _match_client_profile(ua):
    ua_l = (ua or "").lower()
    for prof in _client_profiles():
        if not prof.get("enabled"): continue
        pats = [x.strip().lower() for x in (prof.get("ua", "") or "").split(",") if x.strip()]
        if any(pat in ua_l for pat in pats):
            return prof
    return None

def _render_client_profile(prof, nodes, user_data):
    """Kullanicinin yazdigi sablonu yer tutucularla doldurur. Placeholder'lar:
    {links} {links_json} {links_base64} {count} ve user_data anahtarlari ({username},{used},{limit},...)."""
    links = "\n".join(nodes)
    repl = {
        "{links}": links,
        "{links_json}": json.dumps(nodes, ensure_ascii=False),
        "{links_base64}": base64.b64encode(links.encode("utf-8")).decode("utf-8"),
        "{count}": str(len(nodes)),
    }
    for k, v in (user_data or {}).items():
        repl["{" + str(k) + "}"] = str(v)
    tpl = prof.get("tpl", "") or ""
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl

# ---- Node URI -> normalize + Clash / sing-box donusturucu ----
def _parse_node(uri):
    uri = uri.strip()
    try:
        if uri.startswith("vmess://"):
            d = json.loads(base64.b64decode(uri[8:] + "===").decode("utf-8"))
            return {"proto": "vmess", "name": d.get("ps", ""), "server": d.get("add", ""),
                    "port": int(d.get("port", 443) or 443), "uuid": d.get("id", ""),
                    "alterId": int(d.get("aid", 0) or 0), "cipher": d.get("scy", "auto") or "auto",
                    "net": d.get("net", "tcp") or "tcp", "tls": (d.get("tls", "") == "tls"),
                    "sni": d.get("sni", "") or d.get("host", ""), "ws_path": d.get("path", "/") or "/",
                    "ws_host": d.get("host", "")}
        p = urllib.parse.urlparse(uri)
        name = urllib.parse.unquote(p.fragment) if p.fragment else ""
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        if p.scheme in ("vless", "trojan"):
            return {"proto": p.scheme, "name": name, "server": p.hostname or "", "port": int(p.port or 443),
                    "uuid": urllib.parse.unquote(p.username or ""), "net": qs.get("type", "tcp") or "tcp",
                    "security": qs.get("security", "none") or "none",
                    "sni": qs.get("sni", qs.get("host", p.hostname or "")), "ws_path": qs.get("path", "/") or "/",
                    "ws_host": qs.get("host", ""), "grpc": qs.get("serviceName", ""), "flow": qs.get("flow", ""),
                    "fp": qs.get("fp", "chrome") or "chrome", "pbk": qs.get("pbk", ""), "sid": qs.get("sid", "")}
        if p.scheme == "ss":
            body = uri[5:].split("#")[0]
            if "@" in body:
                mp, hp = body.split("@", 1)
                try: mp = base64.b64decode(mp + "===").decode("utf-8")
                except: pass
                method, pwd = mp.split(":", 1); host, port = hp.rsplit(":", 1)
            else:
                dec = base64.b64decode(body + "===").decode("utf-8")
                mp, hp = dec.split("@", 1); method, pwd = mp.split(":", 1); host, port = hp.rsplit(":", 1)
            return {"proto": "ss", "name": name, "server": host, "port": int(port.split("/")[0]), "method": method, "password": pwd}
    except: return None
    return None

def _node_to_clash(n, tag):
    t = n["proto"]
    if t == "ss":
        return {"name": tag, "type": "ss", "server": n["server"], "port": n["port"], "cipher": n["method"], "password": n["password"], "udp": True}
    if t == "trojan":
        o = {"name": tag, "type": "trojan", "server": n["server"], "port": n["port"], "password": n["uuid"], "udp": True, "sni": n.get("sni", ""), "skip-cert-verify": True}
        if n.get("net") == "ws": o["network"] = "ws"; o["ws-opts"] = {"path": n.get("ws_path", "/"), **({"headers": {"Host": n["ws_host"]}} if n.get("ws_host") else {})}
        return o
    if t == "vmess":
        o = {"name": tag, "type": "vmess", "server": n["server"], "port": n["port"], "uuid": n["uuid"], "alterId": n.get("alterId", 0), "cipher": n.get("cipher", "auto"), "udp": True, "tls": bool(n.get("tls")), "servername": n.get("sni", "")}
        if n.get("net") == "ws": o["network"] = "ws"; o["ws-opts"] = {"path": n.get("ws_path", "/"), **({"headers": {"Host": n["ws_host"]}} if n.get("ws_host") else {})}
        return o
    if t == "vless":
        sec = n.get("security", "none")
        o = {"name": tag, "type": "vless", "server": n["server"], "port": n["port"], "uuid": n["uuid"], "udp": True, "tls": sec in ("tls", "reality"), "network": n.get("net", "tcp"), "servername": n.get("sni", ""), "client-fingerprint": n.get("fp", "chrome")}
        if n.get("flow"): o["flow"] = n["flow"]
        if sec == "reality": o["reality-opts"] = {"public-key": n.get("pbk", ""), "short-id": n.get("sid", "")}
        if n.get("net") == "ws": o["ws-opts"] = {"path": n.get("ws_path", "/"), **({"headers": {"Host": n["ws_host"]}} if n.get("ws_host") else {})}
        elif n.get("net") == "grpc": o["grpc-opts"] = {"grpc-service-name": n.get("grpc", "")}
        if sec not in ("tls", "reality"): o["skip-cert-verify"] = True
        return o
    return None

def _node_to_singbox(n, tag):
    t = n["proto"]
    def tls(enabled):
        d = {"enabled": True, "server_name": n.get("sni", ""), "insecure": True}
        if n.get("security") == "reality": d["reality"] = {"enabled": True, "public_key": n.get("pbk", ""), "short_id": n.get("sid", "")}; d.pop("insecure", None)
        if n.get("fp"): d["utls"] = {"enabled": True, "fingerprint": n.get("fp", "chrome")}
        return d
    def transport():
        if n.get("net") == "ws": return {"type": "ws", "path": n.get("ws_path", "/"), **({"headers": {"Host": n["ws_host"]}} if n.get("ws_host") else {})}
        if n.get("net") == "grpc": return {"type": "grpc", "service_name": n.get("grpc", "")}
        return None
    if t == "ss":
        return {"type": "shadowsocks", "tag": tag, "server": n["server"], "server_port": n["port"], "method": n["method"], "password": n["password"]}
    if t == "trojan":
        o = {"type": "trojan", "tag": tag, "server": n["server"], "server_port": n["port"], "password": n["uuid"]}
        if n.get("net") == "ws": o["transport"] = transport()
        o["tls"] = {"enabled": True, "server_name": n.get("sni", ""), "insecure": True}
        return o
    if t == "vmess":
        o = {"type": "vmess", "tag": tag, "server": n["server"], "server_port": n["port"], "uuid": n["uuid"], "alter_id": n.get("alterId", 0), "security": n.get("cipher", "auto")}
        tr = transport()
        if tr: o["transport"] = tr
        if n.get("tls"): o["tls"] = {"enabled": True, "server_name": n.get("sni", ""), "insecure": True}
        return o
    if t == "vless":
        o = {"type": "vless", "tag": tag, "server": n["server"], "server_port": n["port"], "uuid": n["uuid"]}
        if n.get("flow"): o["flow"] = n["flow"]
        tr = transport()
        if tr: o["transport"] = tr
        if n.get("security") in ("tls", "reality"): o["tls"] = tls(True)
        return o
    return None

def _uniq_tags(names):
    seen = {}; out = []
    for nm in names:
        nm = (nm or "node").strip() or "node"
        if nm in seen: seen[nm] += 1; nm = f"{nm} ({seen[nm]})"
        else: seen[nm] = 0
        out.append(nm)
    return out

def _filter_nodes(nodes, protos):
    if not protos: return nodes
    out = []
    for u in nodes:
        low = u.lower()
        sc = low.split("://", 1)[0]
        tok = {"ss": "ss", "vmess": "vmess", "vless": "vless", "trojan": "trojan", "hysteria2": "hysteria2", "hy2": "hysteria2", "tuic": "tuic"}.get(sc)
        if tok in protos: out.append(u)
    return out

def _emit_clash(nodes, prof):
    parsed = [(_parse_node(u), u) for u in nodes]
    parsed = [(n, u) for n, u in parsed if n]
    tags = _uniq_tags([n.get("name") or n.get("server") for n, _ in parsed])
    proxies = []
    for (n, _), tag in zip(parsed, tags):
        c = _node_to_clash(n, tag)
        if c: proxies.append(c)
    names = [c["name"] for c in proxies]
    groups = []
    if prof.get("balancer") and names:
        bname = prof.get("balancer_name") or "⚡ Auto"
        groups.append({"name": bname, "type": "url-test", "proxies": names, "url": "http://www.gstatic.com/generate_204", "interval": 300, "tolerance": 50})
        sel = [bname] + names
    else:
        sel = names
    groups.insert(0, {"name": "PROXY", "type": "select", "proxies": sel or ["DIRECT"]})
    cfg = {"mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "info",
           "proxies": proxies, "proxy-groups": groups, "rules": ["MATCH,PROXY"]}
    return json.dumps(cfg, ensure_ascii=False, indent=2)

def _emit_singbox(nodes, prof):
    parsed = [_parse_node(u) for u in nodes]
    parsed = [n for n in parsed if n]
    tags = _uniq_tags([n.get("name") or n.get("server") for n in parsed])
    outs = []
    for n, tag in zip(parsed, tags):
        o = _node_to_singbox(n, tag)
        if o: outs.append(o)
    tag_list = [o["tag"] for o in outs]
    extra = []
    if prof.get("balancer") and tag_list:
        bname = prof.get("balancer_name") or "auto"
        extra.append({"type": "urltest", "tag": bname, "outbounds": tag_list, "url": "http://www.gstatic.com/generate_204", "interval": "5m"})
        sel_out = [bname] + tag_list
    else:
        sel_out = tag_list
    selector = {"type": "selector", "tag": "proxy", "outbounds": sel_out or ["direct"]}
    cfg = {"outbounds": [selector] + extra + outs + [{"type": "direct", "tag": "direct"}]}
    return json.dumps(cfg, ensure_ascii=False, indent=2)

_XRAY_INBOUNDS = [
    {"tag": "socks", "port": 10808, "listen": "0.0.0.0", "protocol": "socks",
     "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": False},
     "settings": {"auth": "noauth", "udp": True, "allowTransparent": False}},
    {"tag": "http", "port": 10809, "listen": "0.0.0.0", "protocol": "http",
     "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": False},
     "settings": {"auth": "noauth", "udp": True, "allowTransparent": False}},
]

_MZ_EMAIL = "https://gozargah.github.io/marzban/"

def _apply_xray_stream(ss, net, sec, qs, host_default):
    host = qs.get("host", ""); sni = qs.get("sni", host or host_default)
    fp = qs.get("fp", "chrome") or "chrome"
    alpn = [a for a in qs.get("alpn", "").split(",") if a]
    if net == "ws":
        ss["wsSettings"] = {"headers": {"Pragma": "no-cache"}, "path": qs.get("path", "/"), "host": host or sni}
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": qs.get("serviceName", "")}
    elif net == "xhttp":
        xs = {"headers": {"Pragma": "no-cache"}, "mode": qs.get("mode", "auto"), "path": qs.get("path", "/"), "host": host}
        extra = qs.get("extra", "")
        if extra:
            try: xs.update(json.loads(extra))
            except: pass
        ss["xhttpSettings"] = xs
    if sec == "tls":
        t = {"serverName": sni, "allowInsecure": False, "fingerprint": fp, "show": False}
        if alpn: t["alpn"] = alpn
        ss["tlsSettings"] = t
    elif sec == "reality":
        ss["realitySettings"] = {"serverName": sni, "fingerprint": fp, "show": False, "publicKey": qs.get("pbk", ""), "shortId": qs.get("sid", "")}

def _node_to_xray_outbound(uri, tag):
    """Node URI -> Marzban/Happ ile BIREBIR uyumlu xray outbound (ws host, xhttp, reality, hysteria dahil)."""
    uri = uri.strip()
    try:
        if uri.startswith("vmess://"):
            d = json.loads(base64.b64decode(uri[8:] + "===").decode("utf-8"))
            net = d.get("net", "tcp") or "tcp"; tls = (d.get("tls", "") == "tls")
            ss = {"network": net, "security": "tls" if tls else "none"}
            if net == "ws":
                ss["wsSettings"] = {"headers": {"Pragma": "no-cache"}, "path": d.get("path", "/") or "/", "host": d.get("host", "") or d.get("add", "")}
            elif net == "grpc":
                ss["grpcSettings"] = {"serviceName": d.get("path", "")}
            if tls:
                ss["tlsSettings"] = {"serverName": d.get("sni", "") or d.get("host", "") or d.get("add", ""), "allowInsecure": False, "fingerprint": "chrome", "show": False}
            return {"protocol": "vmess", "settings": {"vnext": [{"address": d.get("add", ""), "port": int(d.get("port", 443) or 443), "users": [{"id": d.get("id", ""), "security": d.get("scy", "auto") or "auto", "alterId": int(d.get("aid", 0) or 0), "email": _MZ_EMAIL}]}]}, "streamSettings": ss, "tag": tag}
        p = urllib.parse.urlparse(uri)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        if p.scheme == "vless":
            net = qs.get("type", "tcp") or "tcp"; sec = qs.get("security", "none") or "none"
            user = {"id": urllib.parse.unquote(p.username or ""), "security": "auto", "encryption": qs.get("encryption", "none") or "none", "email": _MZ_EMAIL, "alterId": 0, "flow": qs.get("flow", "")}
            ss = {"network": net, "security": sec}
            _apply_xray_stream(ss, net, sec, qs, p.hostname or "")
            return {"protocol": "vless", "settings": {"vnext": [{"address": p.hostname or "", "port": int(p.port or 443), "users": [user]}]}, "streamSettings": ss, "tag": tag}
        if p.scheme == "trojan":
            net = qs.get("type", "tcp") or "tcp"; sec = qs.get("security", "tls") or "tls"
            if sec == "none": sec = "tls"
            ss = {"network": net, "security": sec}
            _apply_xray_stream(ss, net, sec, qs, p.hostname or "")
            return {"protocol": "trojan", "settings": {"servers": [{"address": p.hostname or "", "port": int(p.port or 443), "password": urllib.parse.unquote(p.username or ""), "email": _MZ_EMAIL}]}, "streamSettings": ss, "tag": tag}
        if p.scheme == "ss":
            n = _parse_node(uri)
            if not n: return None
            return {"protocol": "shadowsocks", "settings": {"servers": [{"address": n["server"], "port": n["port"], "password": n["password"], "email": _MZ_EMAIL, "method": n["method"], "uot": False}]}, "streamSettings": {"network": "tcp", "tcpSettings": {"header": {"type": "none"}}}, "tag": tag}
        if p.scheme in ("hysteria2", "hy2"):
            host = p.hostname or ""; port = int(p.port or 443); auth = urllib.parse.unquote(p.username or "")
            alpn = [a for a in qs.get("alpn", "").split(",") if a] or ["h3"]
            ss = {"network": "hysteria", "security": "tls",
                  "tlsSettings": {"serverName": qs.get("sni", host), "allowInsecure": False, "alpn": alpn, "show": False},
                  "hysteriaSettings": {"version": 2, "auth": auth}}
            if qs.get("obfs") == "salamander" or qs.get("obfs-password"):
                ss["finalmask"] = {"udp": [{"type": "salamander", "settings": {"password": qs.get("obfs-password", "")}}]}
            return {"protocol": "hysteria", "settings": {"version": 2, "address": host, "port": port}, "streamSettings": ss, "tag": tag}
    except: return None
    return None

def _json_direct_rules():
    """Panel ayarlarindan 'direct siteler' + 'reklam engelle' routing kurallarini uretir."""
    rules = []
    doms = [l.strip() for l in get_global("json_direct_domains", "").split("\n") if l.strip()]
    if doms: rules.append({"type": "field", "domain": doms, "outboundTag": "direct"})
    ips = [l.strip() for l in get_global("json_direct_ips", "").split("\n") if l.strip()]
    if ips: rules.append({"type": "field", "ip": ips, "outboundTag": "direct"})
    if get_global("json_block_ads", "0") == "1":
        rules.append({"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "block"})
    return rules

def _emit_xrayjson(nodes):
    """v2rayN / Happ 'xray-json' abonelik formati: her node tam bir Xray config objesi,
    + opsiyonel otomatik (balancer) config. Routing kurallari panel ayarindan gelir."""
    direct = {"tag": "direct", "protocol": "freedom"}
    block = {"tag": "block", "protocol": "blackhole"}
    base_rules = _json_direct_rules()
    configs = []
    obs_all = []  # balancer icin tum proxy outbound'lar (hysteria dahil)
    for i, uri in enumerate(nodes):
        ob = _node_to_xray_outbound(uri, "proxy")
        if not ob: continue
        configs.append({
            "log": {"access": "", "error": "", "loglevel": "warning"},
            "inbounds": _XRAY_INBOUNDS,
            "outbounds": [ob, direct, block],
            "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
            "routing": {"domainStrategy": "AsIs", "rules": list(base_rules)},
            "remarks": _display_name(uri),
        })
        obs_all.append(uri)

    # Otomatik config (balancer): referans Marzban yapisi — tag "auto", proxy-N (1-indeksli), leastPing
    if get_global("json_balancer", "0") == "1" and obs_all:
        bobs = []
        for i, uri in enumerate(obs_all):
            ob = _node_to_xray_outbound(uri, f"proxy-{i+1}")
            if ob: bobs.append(ob)
        if bobs:
            strategy = get_global("json_balancer_strategy", "leastPing") or "leastPing"
            interval = get_global("json_balancer_interval", "1m") or "1m"
            probe = get_global("json_balancer_url", "https://www.gstatic.com/generate_204") or "https://www.gstatic.com/generate_204"
            bcfg = {
                "log": {"access": "", "error": "", "loglevel": "warning"},
                "inbounds": _XRAY_INBOUNDS,
                "outbounds": bobs + [direct, block],
                "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
                "observatory": {"subjectSelector": ["proxy-"], "probeUrl": probe, "probeInterval": interval, "enableConcurrency": True},
                "routing": {"domainStrategy": "AsIs",
                            "rules": list(base_rules) + [{"type": "field", "network": "tcp,udp", "balancerTag": "auto"}],
                            "balancers": [{"tag": "auto", "selector": ["proxy-"], "strategy": {"type": strategy}, "fallbackTag": "proxy-1"}]},
                "remarks": get_global("json_balancer_name", "⚡ Auto (fastest)") or "⚡ Auto (fastest)",
            }
            if get_global("json_balancer_pos", "first") == "first": configs.insert(0, bcfg)
            else: configs.append(bcfg)
    return json.dumps(configs, ensure_ascii=False, indent=2)

def _ua_wants_json(ua):
    pats = [x.strip().lower() for x in get_global("json_apps", "").split(",") if x.strip()]
    ua_l = (ua or "").lower()
    return any(p in ua_l for p in pats)

def _xray_wrap(ob, remarks, base_rules):
    ob = dict(ob); ob["tag"] = "proxy"
    if "streamSettings" not in ob:
        ob["streamSettings"] = {"network": "tcp", "tcpSettings": {"header": {"type": "none"}}}
    return {"log": {"access": "", "error": "", "loglevel": "warning"}, "inbounds": _XRAY_INBOUNDS,
            "outbounds": [ob, {"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
            "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
            "routing": {"domainStrategy": "AsIs", "rules": list(base_rules)}, "remarks": remarks}

def _proxy_ob_of(cfg):
    for o in cfg.get("outbounds", []):
        if o.get("tag") == "proxy" or o.get("protocol") not in ("freedom", "blackhole", "dns"):
            return o
    return None

async def _build_json_sub(url, extra_links):
    """JSON modu: ustteki Marzban'in kendi xray-json'ini alir (tum ws/xhttp/reality/hysteria
    DOGRU gelir), macros2 routing + balancer ekler, harici/manuel node'lari da ekler."""
    base_rules = _json_direct_rules()
    direct = {"tag": "direct", "protocol": "freedom"}
    block = {"tag": "block", "protocol": "blackhole"}
    mz = []
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as cl:
            r = await cl.get(url, headers={"User-Agent": "Happ/3.24.1"})
            data = r.json()
            if isinstance(data, list): mz = data
    except: mz = []

    configs = []
    proxy_obs = []  # balancer icin tum proxy outbound'lari
    for cfg in mz:
        if not isinstance(cfg, dict): continue
        ob = _proxy_ob_of(cfg)
        if ob is not None: proxy_obs.append(ob)
        cfg["routing"] = {"domainStrategy": "AsIs", "rules": list(base_rules)}  # macros2 routing
        if "dns" not in cfg: cfg["dns"] = {"servers": ["1.1.1.1", "8.8.8.8"]}
        configs.append(cfg)

    # Harici + manuel node'lar (macros2'ye ozel) — best-effort cevrilir
    for uri in (extra_links or []):
        ob = parse_link_for_xray(uri)
        if not ob: continue
        proxy_obs.append(ob)
        configs.append(_xray_wrap(ob, _display_name(uri), base_rules))

    # Otomatik config (balancer): tum proxy outbound'lari tek config'te toplar
    if get_global("json_balancer", "0") == "1" and proxy_obs:
        bobs = []
        for i, o in enumerate(proxy_obs):
            oc = dict(o); oc["tag"] = f"proxy-{i}"
            if "streamSettings" not in oc: oc["streamSettings"] = {"network": "tcp", "tcpSettings": {"header": {"type": "none"}}}
            bobs.append(oc)
        strategy = get_global("json_balancer_strategy", "leastPing") or "leastPing"
        interval = get_global("json_balancer_interval", "1m") or "1m"
        probe = get_global("json_balancer_url", "https://www.gstatic.com/generate_204") or "https://www.gstatic.com/generate_204"
        bcfg = {"log": {"access": "", "error": "", "loglevel": "warning"}, "inbounds": _XRAY_INBOUNDS,
                "outbounds": bobs + [direct, block], "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
                "observatory": {"subjectSelector": ["proxy-"], "probeUrl": probe, "probeInterval": interval, "enableConcurrency": True},
                "routing": {"domainStrategy": "AsIs",
                            "balancers": [{"tag": "balancer", "selector": ["proxy-"], "strategy": {"type": strategy}}],
                            "rules": list(base_rules) + [{"type": "field", "balancerTag": "balancer", "network": "tcp,udp"}]},
                "remarks": get_global("json_balancer_name", "⚡ Auto (fastest)") or "⚡ Auto (fastest)"}
        if get_global("json_balancer_pos", "first") == "first": configs.insert(0, bcfg)
        else: configs.append(bcfg)
    return json.dumps(configs, ensure_ascii=False, indent=2)

def _directives_to_headers(prefix_text):
    """Link modunda body'ye yazilan #profile-title/#announce/#support-url gibi direktifleri
    JSON modu icin HTTP header'a cevirir (JSON govdesinde bunlara yer yok)."""
    hdr = {}
    for line in (prefix_text or "").split("\n"):
        m = re.match(r'^#([a-zA-Z0-9_-]+):\s*(.*)$', line.strip())
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if key in ("profile-title", "announce", "support-url", "profile-web-page-url", "profile-update-interval"):
                try: val.encode("latin-1"); hdr[key] = val
                except: pass
    return hdr

def _emit_profile(prof, nodes, user_data):
    """Profilin formatina gore (body, content_type) uretir."""
    fmt = prof.get("format", "raw_b64") or "raw_b64"
    nodes = _filter_nodes(nodes, set(prof.get("protocols") or []))
    ctype = prof.get("ctype") or ""
    if fmt == "custom":
        return _render_client_profile(prof, nodes, user_data), (ctype or "text/plain; charset=utf-8")
    if fmt == "raw_plain":
        return "\n".join(nodes), (ctype or "text/plain; charset=utf-8")
    if fmt == "ss_only":
        return "\n".join(x for x in nodes if x.lower().startswith("ss://")), (ctype or "text/plain; charset=utf-8")
    if fmt == "json":
        return _emit_xrayjson(nodes), (ctype or "application/json; charset=utf-8")
    if fmt == "json_links":
        return json.dumps(nodes, ensure_ascii=False, indent=2), (ctype or "application/json; charset=utf-8")
    if fmt == "clash":
        return _emit_clash(nodes, prof), (ctype or "text/yaml; charset=utf-8")
    if fmt == "singbox":
        return _emit_singbox(nodes, prof), (ctype or "application/json; charset=utf-8")
    body = "\n".join(nodes)
    return base64.b64encode(body.encode("utf-8")).decode("utf-8"), (ctype or "text/plain; charset=utf-8")
# --- ISTEMCI PROFILLERI BITIS ---

async def check_single_proxy(outb, timeout=3.0):
    return (await proxy_latency_ms(outb, timeout)) is not None

async def proxy_latency_ms(outb, timeout=3.0):
    """Node'u yerel Xray uzerinden gercek istekle dener (v2rayN/Happ 'real delay' gibi):
    calisirsa gecikme (ms), calismazsa None."""
    if not outb: return None
    base_port = random.randint(20000, 60000)
    inbounds = [{"listen": "127.0.0.1", "port": base_port, "protocol": "http"}]
    outbounds = [outb]
    cfg_path = f"/tmp/xtest_{base_port}_{random.randint(1000,9999)}.json"
    with open(cfg_path, "w") as f: json.dump({"inbounds": inbounds, "outbounds": outbounds}, f)
    proc, alive = None, False
    try:
        proc = await asyncio.create_subprocess_exec(os.path.join(BASE_DIR, "xray", "xray"), "-c", cfg_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.sleep(0.3)
        if proc.returncode is None:
            curl_proc = await asyncio.create_subprocess_exec("curl", "-x", f"http://127.0.0.1:{base_port}", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "-m", str(timeout), "http://www.gstatic.com/generate_204", stdout=asyncio.subprocess.PIPE)
            stdout, _ = await curl_proc.communicate()
            parts = (stdout or b"").decode().split()
            if parts and parts[0] in ("200", "204"):
                try: alive = max(1, int(float(parts[1]) * 1000))
                except Exception: alive = 1
    except: pass
    finally:
        if proc and proc.returncode is None:
            try: proc.kill(); await proc.wait()
            except: pass
        try: os.remove(cfg_path)
        except: pass
    return alive if alive else None

def _ping_max_ms(item=None):
    """Gecikme limiti (ms): slotta ayarliysa o, yoksa global; 0 = limit yok."""
    try:
        v = int(float((item or {}).get("max_ms") or 0))
        if v > 0: return v
    except Exception: pass
    try: return max(0, int(float(get_global("ping_max_ms", "0") or 0)))
    except Exception: return 0

async def _node_latency(outb, timeout, api_online=False):
    """Gecikme (ms) ya da None (olu) — yerel Xray ile gercek istek."""
    return await proxy_latency_ms(outb, timeout)

def auto_rename_node(line, cdn_label="", vps_label=""):
    line = line.strip()
    if not line or line.startswith("#") or "://" not in line: return line
    flag_match = re.search(r'[\U0001F1E6-\U0001F1FF]{2}', line)
    flag = flag_match.group(0) if flag_match else None
    
    if line.startswith("vmess://"):
        try:
            d = json.loads(base64.b64decode(line[8:]+"==").decode("utf-8"))
            if not flag and (fm := re.search(r'[\U0001F1E6-\U0001F1FF]{2}', d.get("ps", ""))): flag = fm.group(0)
        except: pass
    else:
        if "#" in line:
            # .find("#") ile en baştaki ayırıcıyı bulur, serverDescription vs. çöplerini yok eder.
            if not flag and (fm := re.search(r'[\U0001F1E6-\U0001F1FF]{2}', urllib.parse.unquote(line[line.find("#")+1:]))): flag = fm.group(0)
            
    if not flag or flag not in FLAG_MAP: flag, country_cap = random.choice(list(FLAG_MAP.items()))
    else: country_cap = FLAG_MAP[flag]
    
    country_name = country_cap.split(" - ")[0]
    low = line.lower()
    
    # xHTTP CDN ve 4G/WiFi mantığı
    if "type=xhttp" in low: new_name = f"{flag} {country_name} [CDN]"
    elif line.startswith("vless://") and ("type=ws" in low or "net=ws" in low):
        new_name = f"{flag} 4G/WiFi"
        if cdn_label: new_name += f" {cdn_label}"
    else:
        new_name = f"{flag} {country_cap}"
        if vps_label: new_name += f" {vps_label}"
        
    if line.startswith("vmess://"):
        try:
            d = json.loads(base64.b64decode(line[8:]+"==").decode("utf-8"))
            d["ps"] = new_name
            return f"vmess://{base64.b64encode(json.dumps(d, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).decode('utf-8')}"
        except: return line
    else:
        base_uri = line[:line.find("#")] if "#" in line else line
        return f"{base_uri}#{urllib.parse.quote(new_name)}"

def _xray_ob_to_link(ob, name):
    """Xray outbound -> paylasim linki (vless/vmess/trojan/ss/hysteria2). Desteklenmezse None."""
    proto = ob.get("protocol"); st = ob.get("settings") or {}; ss = ob.get("streamSettings") or {}
    net = ss.get("network") or "tcp"; sec = ss.get("security") or "none"
    q = {"type": net}
    if sec in ("tls", "reality"):
        q["security"] = sec
        t = ss.get("tlsSettings") or ss.get("realitySettings") or {}
        if t.get("serverName"): q["sni"] = t["serverName"]
        if t.get("fingerprint"): q["fp"] = t["fingerprint"]
        if t.get("alpn"): q["alpn"] = ",".join(t["alpn"]) if isinstance(t["alpn"], list) else t["alpn"]
        if sec == "reality":
            if t.get("publicKey"): q["pbk"] = t["publicKey"]
            if t.get("shortId") is not None: q["sid"] = t.get("shortId") or ""
            if t.get("spiderX"): q["spx"] = t["spiderX"]
        if t.get("allowInsecure"): q["allowInsecure"] = "1"
    else:
        q["security"] = "none"
    if net == "ws":
        w = ss.get("wsSettings") or {}
        q["path"] = w.get("path", "/"); h = w.get("host") or (w.get("headers") or {}).get("Host")
        if h: q["host"] = h
    elif net == "grpc":
        g = ss.get("grpcSettings") or {}
        q["serviceName"] = g.get("serviceName", "")
        if g.get("multiMode"): q["mode"] = "multi"
    elif net in ("xhttp", "splithttp"):
        x = ss.get("xhttpSettings") or ss.get("splithttpSettings") or {}
        q["type"] = "xhttp"; q["path"] = x.get("path", "/")
        if x.get("host"): q["host"] = x["host"]
        if x.get("mode"): q["mode"] = x["mode"]
    elif net == "httpupgrade":
        x = ss.get("httpupgradeSettings") or {}
        q["path"] = x.get("path", "/")
        if x.get("host"): q["host"] = x["host"]
    elif net == "tcp":
        hd = ((ss.get("tcpSettings") or {}).get("header") or {})
        if hd.get("type") == "http":
            q["headerType"] = "http"
            req = hd.get("request") or {}
            if req.get("path"): q["path"] = req["path"][0] if isinstance(req["path"], list) else req["path"]
            hh = (req.get("headers") or {}).get("Host")
            if hh: q["host"] = hh[0] if isinstance(hh, list) else hh
    frag = "#" + urllib.parse.quote(name or ob.get("tag") or "node")
    qs = lambda d: urllib.parse.urlencode({k: v for k, v in d.items() if v is not None}, safe="/,:")
    try:
        if proto in ("vless", "vmess"):
            vn = (st.get("vnext") or [{}])[0]; u = (vn.get("users") or [{}])[0]
            addr, port, uid = vn.get("address"), vn.get("port"), u.get("id")
            if not (addr and port and uid): return None
            if proto == "vless":
                q["encryption"] = u.get("encryption") or "none"
                if u.get("flow"): q["flow"] = u["flow"]
                return f"vless://{uid}@{addr}:{port}?{qs(q)}{frag}"
            vm = {"v": "2", "ps": name or ob.get("tag") or "node", "add": addr, "port": str(port), "id": uid,
                  "aid": str(u.get("alterId", 0)), "scy": u.get("security", "auto"), "net": q.get("type", "tcp"),
                  "type": q.get("headerType", "none"), "host": q.get("host", ""), "path": q.get("path", q.get("serviceName", "")),
                  "tls": "tls" if sec == "tls" else "", "sni": q.get("sni", ""), "fp": q.get("fp", ""), "alpn": q.get("alpn", "")}
            return "vmess://" + base64.b64encode(json.dumps(vm, ensure_ascii=False).encode()).decode()
        if proto == "trojan":
            sv = (st.get("servers") or [{}])[0]
            if not (sv.get("address") and sv.get("port") and sv.get("password")): return None
            return f"trojan://{urllib.parse.quote(sv['password'], safe='')}@{sv['address']}:{sv['port']}?{qs(q)}{frag}"
        if proto == "shadowsocks":
            sv = (st.get("servers") or [{}])[0]
            if not (sv.get("address") and sv.get("port") and sv.get("password")): return None
            ui = base64.urlsafe_b64encode(f"{sv.get('method', 'chacha20-ietf-poly1305')}:{sv['password']}".encode()).decode().rstrip("=")
            return f"ss://{ui}@{sv['address']}:{sv['port']}{frag}"
        if proto in ("hysteria2", "hysteria"):
            sv = (st.get("servers") or [st])[0]
            addr, port = sv.get("address"), sv.get("port")
            pw = sv.get("password") or (ss.get("hysteriaSettings") or {}).get("auth")
            if not (addr and port and pw): return None
            hq = {k: q[k] for k in ("sni", "alpn") if k in q}
            if q.get("allowInsecure"): hq["insecure"] = "1"
            return f"hysteria2://{urllib.parse.quote(str(pw), safe='')}@{addr}:{port}?{qs(hq)}{frag}"
    except Exception:
        return None
    return None

def _xrayjson_to_links(text):
    """Happ/Remnawave/Marzban xray-json abonelik (tek config ya da liste) -> link listesi."""
    try: data = json.loads(text)
    except Exception: return None
    cfgs = data if isinstance(data, list) else [data]
    out, seen = [], set()
    for c in cfgs:
        if not isinstance(c, dict): continue
        obs = [o for o in (c.get("outbounds") or []) if isinstance(o, dict) and o.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks", "hysteria2", "hysteria")]
        for o in obs:
            nm = c.get("remarks") or o.get("tag") or ""
            if len(obs) > 1 and o.get("tag") and o.get("tag") not in ("proxy",): nm = f"{nm} {o['tag']}".strip()
            l = _xray_ob_to_link(o, nm)
            if l and l.split("#")[0] not in seen:
                seen.add(l.split("#")[0]); out.append(l)
    return out

def decode_sub_content(raw_bytes):
    from urllib.parse import unquote
    def dec(t):
        lines = t.split("\n"); result = []
        for line in lines:
            line = line.strip()
            if not line: continue
            if "#" in line and "://" in line: result.append(line[:line.index("#")+1] + unquote(line[line.index("#")+1:]))
            else: result.append(line)
        return "\n".join(result)
    try:
        t = raw_bytes.decode("utf-8").strip()
        if t[:1] in ("[", "{"):  # xray-json abonelik (Happ UA ile cekince bazi paneller JSON verir)
            links = _xrayjson_to_links(t)
            if links: return "\n".join(links), False
        if any(t.startswith(p) for p in ["vless://","ss://","trojan://","vmess://","hysteria","tuic","#"]): 
            return dec(t), False
        clean_t = t.replace("\n", "").replace("\r", "").replace(" ", "")
        clean_t += "=" * ((4 - len(clean_t) % 4) % 4)
        return dec(base64.b64decode(clean_t).decode("utf-8").strip()), True
    except: 
        return raw_bytes.decode("utf-8", errors="replace"), False

def get_vless_subtype(l):
    low = l.lower()
    if "security=reality" in low: return "reality"
    if "type=grpc" in low: return "grpc"
    if "type=xhttp" in low: return "xhttp"
    if "type=ws" in low: return "ws"
    if "type=tcp" in low or "type=" not in low: return "tcp"
    return "other"

def get_vmess_subtype(l):
    try:
        net = json.loads(base64.b64decode(l[len("vmess://"):] + "==").decode("utf-8")).get("net", "tcp").lower()
        if net in ["ws", "grpc", "tcp"]: return net
    except: pass
    return "other"

# SIFIR HATA: Sıralamada eksik bırakılanlar yutulmasın diye 'other' listesi garantilendi
def sort_links(lines, sort_order, vless_sort, vmess_sort):
    buckets = {p: [] for p in sort_order}; buckets["other"] = []
    v_b = {s: [] for s in vless_sort}; v_b["other"] = []
    m_b = {s: [] for s in vmess_sort}; m_b["other"] = []
    
    for l in [x.strip() for x in lines if x.strip()]:
        matched = False
        for p in sort_order:
            if l.startswith(f"{p}://"):
                if p == "vless":
                    sub = get_vless_subtype(l)
                    if sub in v_b: v_b[sub].append(l)
                    else: v_b["other"].append(l)
                elif p == "vmess":
                    sub = get_vmess_subtype(l)
                    if sub in m_b: m_b[sub].append(l)
                    else: m_b["other"].append(l)
                else: buckets[p].append(l)
                matched = True; break
        if not matched: buckets["other"].append(l)
        
    res = []
    for p in sort_order:
        if p == "vless":
            for s in vless_sort: res.extend(v_b[s])
            res.extend(v_b["other"])
        elif p == "vmess":
            for s in vmess_sort: res.extend(m_b[s])
            res.extend(m_b["other"])
        else: res.extend(buckets[p])
    return res + buckets["other"]

def _node_allowed(line, allowed_protos):
    """Bir node satirinin, secili protokol filtresine gore tutulup tutulmayacagi."""
    low = line.lower()
    if low.startswith("ss://"): return "ss" in allowed_protos
    if low.startswith("vmess://"): return "vmess" in allowed_protos
    if low.startswith("trojan://"): return "trojan" in allowed_protos
    if low.startswith("hysteria2://") or low.startswith("hy2://"): return "hysteria2" in allowed_protos
    if low.startswith("vless://"):
        if "type=ws" in low or "net=ws" in low: return "vless_ws" in allowed_protos
        return "vless_tcp" in allowed_protos
    return False

def _display_name(line):
    """Node'un istemcide gorunecek ismi (rename sonrasi) — onizleme icin."""
    try:
        if line.startswith("vmess://"):
            d = json.loads(base64.b64decode(line[len("vmess://"):] + "==").decode("utf-8"))
            return (d.get("ps", "") or "").strip() or "(isimsiz)"
        if "#" in line:
            return urllib.parse.unquote(line[line.find("#")+1:]).strip() or "(isimsiz)"
    except: pass
    return "(isimsiz)"

def _eff_orders(slot_sort):
    """Slotun kendi protokol sirasi varsa onu, yoksa global sirayi dondurur (fallback)."""
    g_o = [x for x in get_global("sort_order", ",".join(PROTOCOLS)).split(",") if x] or PROTOCOLS
    g_v = [x for x in get_global("vless_sort", ",".join(VLESS_SUBTYPES)).split(",") if x] or VLESS_SUBTYPES
    g_m = [x for x in get_global("vmess_sort", ",".join(VMESS_SUBTYPES)).split(",") if x] or VMESS_SUBTYPES
    if slot_sort:
        o = [x for x in (slot_sort.get("sort_order") or "").split(",") if x] or g_o
        v = [x for x in (slot_sort.get("vless_sort") or "").split(",") if x] or g_v
        m = [x for x in (slot_sort.get("vmess_sort") or "").split(",") if x] or g_m
        return o, v, m
    return g_o, g_v, g_m

async def _process_sub_lines(lines, pos, allowed_protos, do_ping, ping_timeout, slot_sort=None, rename=True, max_ms=0):
    """Bir kaynaktan gelen satirlari filtreler, (istege bagli) ping atar ve rename eder.
    Hem URL hem de local kaynaklar bu ayni boru hattindan gecer. Global siralama acikken
    her slot kendi (yoksa global) protokol sirasina gore dizilir."""
    url_top, url_bot = [], []
    sem = asyncio.Semaphore(4)
    cdn_on = get_global("cdn_label_enabled", "0") == "1"
    vps_provs = _vps_enabled_providers()

    api_online = False

    async def process_node(line):
        line = line.strip()
        if not line or line.startswith("#") or "://" not in line: return None
        if not _node_allowed(line, allowed_protos): return None
        if do_ping:
            outb = parse_link_for_xray(line)
            if outb:
                async with sem:
                    lat = await _node_latency(outb, ping_timeout, api_online)
                if lat is None: return None                 # olu
                if max_ms and lat > max_ms: return None     # limitin ustunde -> dahil etme
        if not rename:
            return (line, pos)  # orijinal isim korunur
        cdn_label = await _cdn_label_for(line, cdn_on)
        vps_label = await _vps_label_for(line, vps_provs)
        return (auto_rename_node(line, cdn_label=cdn_label, vps_label=vps_label), pos)

    tasks = [process_node(l) for l in lines]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception): continue
        if r:
            rn, rpos = r
            if rpos == "top": url_top.append(rn)
            else: url_bot.append(rn)
    # Slot-bazli siralama: global siralama acikken her slot kendi/global sirasina dizilir
    if get_global("enable_sort", "1") == "1":
        o, v, m = _eff_orders(slot_sort)
        url_top = sort_links(url_top, o, v, m)
        url_bot = sort_links(url_bot, o, v, m)
    return url_top, url_bot

def _slot_headers(o=None):
    """Slotun istek basliklari: kullanici UA'si (yoksa v2ray) + HWID verildiyse Happ tarzi cihaz basliklari."""
    o = o or {}
    h = {"User-Agent": (o.get("ua") or "").strip() or "v2ray"}
    hw = (o.get("hwid") or "").strip()
    if hw:
        h["x-hwid"] = hw
        h["x-device-os"] = (o.get("dev_os") or "Android").strip()
        h["x-ver-os"] = (o.get("dev_ver") or "14").strip()
        h["x-device-model"] = (o.get("dev_model") or "SM-S918B").strip()
    return h

async def _fetch_sub_lines(url, o=None):
    """Sub'i slotun UA/HWID basliklariyla ceker; node satirlarini dondurur. Hata/bos -> exception."""
    async with httpx.AsyncClient(timeout=12, verify=False, follow_redirects=True) as client:
        resp = await client.get(url, headers=_slot_headers(o))
        resp.raise_for_status()
    content, _ = decode_sub_content(resp.content)
    lines = [l for l in content.split("\n") if "://" in l]
    if not lines: raise RuntimeError("sub boş / kod yok")
    return lines

async def _any_alive(lines, protos, timeout, n=6):
    """Hizli saglik kontrolu: izinli ilk n node'dan biri bile calisiyor mu?"""
    obs = [parse_link_for_xray(l) for l in lines if _node_allowed(l, protos)]
    obs = [o for o in obs if o][:n]
    if not obs: return False
    res = await asyncio.gather(*[proxy_latency_ms(o, timeout) for o in obs], return_exceptions=True)
    return any(isinstance(r, int) for r in res)

_slot_status = {}  # slot anahtari -> {"used": "main"|"fallback"|"fail", "err": str, "ts": t}

async def _fetch_and_process_url(url, pos, allowed_protos, do_ping, ping_timeout, slot_sort=None, rename=True, max_ms=0, opts=None):
    """Ana sub'i isler; ana sub ENGELLENIRSE (hata/bos) ya da icindeki kodlarin HICBIRI calismiyorsa
    slotun fallback sub'ina gecer. Ping kapaliyken saglik icin ilk birkac node hizlica denenir."""
    opts = opts or {}
    fb = (opts.get("fallback") or "").strip()
    err = ""
    try:
        lines = await _fetch_sub_lines(url, opts)
        if fb and not do_ping and not await _any_alive(lines, allowed_protos, ping_timeout):
            raise RuntimeError("ana sub'daki kodların hiçbiri çalışmıyor")
        top, bot = await _process_sub_lines(lines, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, max_ms)
        if top or bot or not fb:
            _slot_status[url] = {"used": "main", "err": "", "ts": time.time()}
            return top, bot
        err = "ana sub'da çalışan kod kalmadı"
    except Exception as e:
        err = str(e)[:120]
        if not fb:
            _slot_status[url] = {"used": "fail", "err": err, "ts": time.time()}
            raise
    try:
        lines = await _fetch_sub_lines(fb, opts)
        top, bot = await _process_sub_lines(lines, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, max_ms)
        _slot_status[url] = {"used": "fallback", "err": err, "ts": time.time()}
        add_panel_log("SLOT_FALLBACK", f"{url[:80]} -> fallback ({err})", "")
        return top, bot
    except Exception as e2:
        _slot_status[url] = {"used": "fail", "err": f"ana: {err} · fallback: {str(e2)[:80]}", "ts": time.time()}
        raise

async def _process_local_source(text, pos, allowed_protos, do_ping, ping_timeout, slot_sort=None, rename=True, max_ms=0):
    """URL yerine elle yapistirilmis node metnini isler (base64 veya duz)."""
    raw = text.encode("utf-8") if isinstance(text, str) else (text or b"")
    content, _ = decode_sub_content(raw)
    return await _process_sub_lines(content.split("\n"), pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, max_ms)

def _slot_sort_from(item):
    """ext_json / istek sozlugunden slot-bazli siralama config'i cikarir (yoksa None)."""
    if any((item.get(k) or "").strip() for k in ("sort_order", "vless_sort", "vmess_sort")):
        return {k: item.get(k, "") for k in ("sort_order", "vless_sort", "vmess_sort")}
    return None

_last_external_fetch_time = 0

async def do_external_fetch():
    global _last_external_fetch_time
    try: subs_data = json.loads(get_global("external_subs_data", "[]"))
    except: subs_data = []
    ping_timeout = float(get_global("ping_timeout", "2.5"))
    results_detail, new_cache = {}, {}
    for i, item in enumerate(subs_data):
        url, pos = item.get("url", "").strip(), item.get("pos", "bottom")
        local = (item.get("local", "") or "")
        allowed_protos = item.get("protocols", [])
        do_ping = item.get("ping", False)
        is_local = (not url) and local.strip()
        if not url and not is_local: continue
        # Her kaynagin benzersiz cache/detay anahtari: URL varsa URL, yoksa local:<sid|index>
        key = url if url else ("local:" + str(item.get("sid") or i))
        slot_sort = _slot_sort_from(item)
        rename = item.get("rename", True) is not False
        try:
            if url:
                u_top, u_bot = await _fetch_and_process_url(url, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, _ping_max_ms(item), item)
            else:
                u_top, u_bot = await _process_local_source(local, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, _ping_max_ms(item))
            new_cache[key] = {"top": u_top, "bottom": u_bot}
            results_detail[key] = len(u_top) + len(u_bot)
        except:
            new_cache[key] = {"top": [], "bottom": []}
            results_detail[key] = 0
    set_global("external_fetched_cache", json.dumps(new_cache))
    _last_external_fetch_time = asyncio.get_event_loop().time()
    return {"total": sum(len(c.get("top",[])) + len(c.get("bottom",[])) for c in new_cache.values()), "details": results_detail}

_last_panel_sync_time = 0
async def fetch_external_subs_loop():
    global _last_external_fetch_time, _last_panel_sync_time
    while True:
        try:
            interval = int(get_global("external_sub_interval", "60"))
            if interval > 0 and (asyncio.get_event_loop().time() - _last_external_fetch_time) >= (interval * 60): await do_external_fetch()
        except: pass
        try:
            psi = int(get_global("panel_sync_seconds", "30") or 30)  # saniye; 0 = kapali
            if psi > 0: psi = max(10, psi)
            if psi > 0 and _effective_panels() and (asyncio.get_event_loop().time() - _last_panel_sync_time) >= psi:
                await _sync_panels()
                _last_panel_sync_time = asyncio.get_event_loop().time()
        except: pass
        await asyncio.sleep(10)

_bg_task = None
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(); global _bg_task; _bg_task = asyncio.create_task(fetch_external_subs_loop()); yield
    if _bg_task: _bg_task.cancel()

app = FastAPI(lifespan=lifespan)
from fastapi.staticfiles import StaticFiles
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")
# allow_origins=["*"] ile allow_credentials=True gecersiz bir kombinasyon (tarayici reddeder).
# Panel ayni-origin cerez auth kullanir; sub endpoint'leri cerezsizdir. credentials=False dogru olan.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def _no_cache_panel(request: Request, call_next):
    resp = await call_next(request)
    # Panel sayfalari asla cache'lenmesin — eski form kaydedince ayarlari silmesin
    if request.url.path.startswith("/panel"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))
templates.env.globals.update(app_name=APP_NAME, app_version=APP_VERSION, author_tg=AUTHOR_TG)
sessions = {}  # in-memory cache; kalici kopya DB'de (restart'ta login dusmez)

def authed(request):
    sid = request.cookies.get("sid")
    if not sid: return None
    if sid in sessions: return sessions[sid]
    with db_session() as con:
        try: row = con.execute("SELECT username FROM panel_sessions WHERE sid=?", (sid,)).fetchone()
        except: row = None
    if row:
        sessions[sid] = {"user": row["username"]}
        return sessions[sid]
    return None

def _session_put(sid, username):
    sessions[sid] = {"user": username}
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO panel_sessions(sid,username) VALUES(?,?) ON CONFLICT(sid) DO UPDATE SET username=?", (sid, username, username)); con.commit()
        except Exception as e: print(f"[DB] session put: {e!r}")

def _session_del(sid):
    sessions.pop(sid, None)
    with DB_LOCK, db_session() as con:
        try: con.execute("DELETE FROM panel_sessions WHERE sid=?", (sid,)); con.commit()
        except: pass
def is_ua_blacklisted(ua):
    with db_session() as con: return any(r["pattern"].lower() in ua.lower() for r in con.execute("SELECT pattern FROM ua_blacklist").fetchall())
def is_ua_whitelisted(ua):
    with db_session() as con: return any(r["pattern"].lower() in ua.lower() for r in con.execute("SELECT pattern FROM ua_whitelist").fetchall())
def extract_device_id(request, ua, ip):
    for h in DEVICE_ID_HEADERS:
        if v := request.headers.get(h,""): return v.strip(), True
    return "fp_" + hashlib.sha256(f"{ua}|{ip}".encode()).hexdigest()[:32], False
def extract_app_name(ua): return ua.split("/")[0][:40] if "/" in ua else (ua[:40] or "unknown")
def extract_device_model(request, ua):
    for h in DEVICE_MODEL_HEADERS:
        if v := request.headers.get(h,""): return v.strip()[:80]
    m = re.search(r'\(([^)]+)\)', ua)
    if m:
        for p in m.group(1).split(";"):
            if (p:=p.strip()) and not any(x in p.lower() for x in ["linux","android","cpu","like mac","u;","wv"]): return p[:60]
        return m.group(1).split(";")[0].strip()[:60]
    return ""
def get_ip(request):
    fwd = request.headers.get("X-Forwarded-For","")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")

from concurrent.futures import ThreadPoolExecutor
_executor = ThreadPoolExecutor(max_workers=4)

def _write_log(s, h, i, u, e, d):
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO logs(sub_id,hardware_id,ip,user_agent,event,detail) VALUES(?,?,?,?,?,?)", (s,h,i,u[:200],e,d)); con.commit()
        except Exception as ex: print(f"[DB] _write_log hatasi: {ex!r}")
def _write_panel_log(e, d, i):
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO panel_logs(event,detail,ip) VALUES(?,?,?)",(e,d,i)); con.commit()
        except Exception as ex: print(f"[DB] _write_panel_log hatasi: {ex!r}")
def _write_traffic(s, nb):
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO traffic(sub_id,bytes_down,req_count) VALUES(?,?,1) ON CONFLICT(sub_id) DO UPDATE SET bytes_down=bytes_down+?,req_count=req_count+1,last_update=datetime('now')", (s,nb,nb)); con.commit()
        except Exception as ex: print(f"[DB] _write_traffic hatasi: {ex!r}")

def add_log(sub_id, event, detail="", hardware_id="", ip="", ua=""):
    try: asyncio.get_event_loop().run_in_executor(_executor, _write_log, sub_id, hardware_id, ip, ua, event, detail)
    except: _write_log(sub_id, hardware_id, ip, ua, event, detail)
def add_panel_log(event, detail="", ip=""):
    try: asyncio.get_event_loop().run_in_executor(_executor, _write_panel_log, event, detail, ip)
    except: _write_panel_log(event, detail, ip)
def update_traffic(sub_id, nb):
    try: asyncio.get_event_loop().run_in_executor(_executor, _write_traffic, sub_id, nb)
    except: _write_traffic(sub_id, nb)

def extract_filename(headers):
    if m := re.search(r'filename=["\']?([^"\';\s]+)["\']?', headers.get("content-disposition","")): return m.group(1).strip()
    return ""
def strip_proxies(c): return "\n".join(l for l in c.split("\n") if l.startswith("#") or not l.strip())

@app.get("/healthcheck")
async def check_health():
    return {"status": "ok"}

@app.get("/macros/{path:path}")
async def sub_proxy_path(request: Request, path: str):
    if not path: raise HTTPException(404, "bulunamadi")
    with db_session() as con: row = con.execute("SELECT url FROM subs WHERE token=? AND token!=''", (path,)).fetchone()
    if not row: raise HTTPException(404, "bulunamadi")
    return await sub_proxy(request, url=row["url"])

@app.get("/macros")
async def sub_proxy(request: Request, url: str = None):
    if not url: raise HTTPException(400, "url eksik")
    ua, ip = request.headers.get("User-Agent",""), get_ip(request)
    hw, is_real_hwid = extract_device_id(request, ua, ip)
    app_name, device_model = extract_app_name(ua), extract_device_model(request, ua)

    allowed_panels = get_global("allowed_panels", "").strip()
    if allowed_panels:
        allowed_list = [p.strip() for p in allowed_panels.split(",") if p.strip()]
        if not any(p in url for p in allowed_list):
            add_log(0, "UNAUTHORIZED_PANEL", f"İzinsiz panel isteği: {url}", hardware_id=hw, ip=ip, ua=ua)
            return PlainTextResponse(get_global("proxy_error_message", "Bu panele erisim izni yok."), status_code=403)

    with db_session() as con: sub = con.execute("SELECT * FROM subs WHERE url=?",(url,)).fetchone()

    friendly_name = ""
    if sub:
        friendly_name = sub["name"]
    else:
        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as cl:
                r = await cl.get(url, headers={"User-Agent": _UPSTREAM_UA}); friendly_name = extract_filename(r.headers)
                if not friendly_name and (pt := r.headers.get("profile-title","")).startswith("base64:"):
                    try: friendly_name = base64.b64decode(pt[7:]).decode().strip()
                    except: pass
        except: pass
            
        if not friendly_name: friendly_name = "sub_" + hashlib.sha256(url.encode()).hexdigest()[:12]

        with DB_LOCK, db_session() as con:
            existing = con.execute("SELECT * FROM subs WHERE name=?", (friendly_name,)).fetchone()
            if existing:
                try: con.execute("UPDATE subs SET url=? WHERE id=?", (url, existing["id"])); con.commit()
                except: pass
                sub = con.execute("SELECT * FROM subs WHERE id=?", (existing["id"],)).fetchone()
                if sub: add_log(sub["id"], "TOKEN_UPDATED", f"Yeni URL aktif: {url.split('/')[-1][:15]}...", hardware_id=hw, ip=ip, ua=ua)
            else:
                def_limit = int(get_global("default_device_limit", "1"))
                try: con.execute("INSERT INTO subs(name,url,device_limit,token) VALUES(?,?,?,?)",(friendly_name,url,def_limit,secrets.token_urlsafe(16))); con.commit()
                except: pass
                sub = con.execute("SELECT * FROM subs WHERE url=?",(url,)).fetchone()
                if sub: add_log(sub["id"],"SUB_AUTO_REGISTER", f"İsim:{friendly_name}",hardware_id=hw,ip=ip,ua=ua)

    if not sub: raise HTTPException(500, "Sunucu İçi Hata (Sub oluşturulamadı)")
    sub_id = sub["id"]

    if sub["blocked"]:
        add_log(sub_id,"BLOCKED_ACCESS","Blokeli sub",hardware_id=hw,ip=ip,ua=ua)
        return PlainTextResponse(sub["blocked_metadata"] or get_global("blocked_metadata",""), status_code=403)
    if is_ua_blacklisted(ua):
        add_log(sub_id,"UA_BLACKLISTED",f"UA:{ua[:100]}",hardware_id=hw,ip=ip,ua=ua)
        return PlainTextResponse("", status_code=403)

    is_unlimited = bool(sub["unlimited"])
    is_forced = bool(sub["force_limit"]) if "force_limit" in sub.keys() else False
    global_limit_active = get_global("device_limit_enabled", "1") == "1"

    limit_active = False
    if not is_unlimited:
        limit_active = True if is_forced else global_limit_active

    with db_session() as con: devs = con.execute("SELECT * FROM devices WHERE sub_id=?",(sub_id,)).fetchall()
    ids = [d["hardware_id"] for d in devs]

    if is_ua_whitelisted(ua):
        if hw not in ids: add_log(sub_id,"UA_WHITELIST_PASS",f"App:{app_name} HW:{hw}",hardware_id=hw,ip=ip,ua=ua)
    elif hw not in ids:
        if limit_active and len(devs) >= sub["device_limit"]:
            add_log(sub_id,"DEVICE_LIMIT_EXCEEDED", f"Limit:{sub['device_limit']} HW:{hw}",hardware_id=hw,ip=ip,ua=ua)
            try:
                async with httpx.AsyncClient(timeout=10, verify=False) as cl:
                    blocked_content, was_b64 = decode_sub_content((await cl.get(url, headers={"User-Agent": _UPSTREAM_UA})).content)
                    b_msg = sub["blocked_device_metadata"] or get_global("blocked_device_metadata", "")
                    if not b_msg: b_msg = "# 🛑 LIMIT EXCEEDED - Cihaz Limiti Asildi"
                    final_block = (b_msg + "\n" + strip_proxies(blocked_content)).strip()
                    if was_b64: final_block = base64.b64encode(final_block.encode('utf-8')).decode('utf-8')
                    return PlainTextResponse(final_block, status_code=200)
            except: return PlainTextResponse("", status_code=403)

        with DB_LOCK, db_session() as con:
            try: con.execute("INSERT INTO devices(sub_id,hardware_id,app_name,device_model,ip,user_agent) VALUES(?,?,?,?,?,?)", (sub_id,hw,app_name,device_model,ip,ua[:200])); con.commit()
            except: pass
        add_log(sub_id,"DEVICE_REGISTERED", f"HW:{hw}", hardware_id=hw,ip=ip,ua=ua)
        asyncio.create_task(sync_single_marzban_user(friendly_name))
    else:
        dev = next((d for d in devs if d["hardware_id"]==hw),None)
        if dev and dev["blocked"]:
            add_log(sub_id,"DEVICE_BLOCKED",f"HW:{hw}",hardware_id=hw,ip=ip,ua=ua)
            try:
                async with httpx.AsyncClient(timeout=10, verify=False) as cl:
                    blocked_content, was_b64 = decode_sub_content((await cl.get(url, headers={"User-Agent": _UPSTREAM_UA})).content)
                    b_msg = sub["blocked_device_metadata"] or get_global("blocked_device_metadata", "")
                    if not b_msg: b_msg = "# 🛑 DEVICE BLOCKED - Cihaziniz Engellendi"
                    final_block = (b_msg + "\n" + strip_proxies(blocked_content)).strip()
                    if was_b64: final_block = base64.b64encode(final_block.encode('utf-8')).decode('utf-8')
                    return PlainTextResponse(final_block, status_code=200)
            except: return PlainTextResponse("", status_code=403)

        with DB_LOCK, db_session() as con:
            try: con.execute("UPDATE devices SET last_seen=datetime('now'),ip=?,user_agent=?,app_name=?,device_model=? WHERE sub_id=? AND hardware_id=?", (ip,ua[:200],app_name,device_model,sub_id,hw)); con.commit()
            except: pass
        asyncio.create_task(sync_single_marzban_user(friendly_name))

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent": _UPSTREAM_UA})
            if resp.status_code == 404:
                add_log(sub_id,"SOURCE_404","Kaynak 404",hardware_id=hw,ip=ip,ua=ua)
                return PlainTextResponse(get_global("source_404_message", "Kaynak bulunamadi."), status_code=200)

            userinfo = resp.headers.get("subscription-userinfo", "")
            used_bytes = 0
            total_bytes = 0
            expire_ts = 0

            if userinfo:
                parts = {k.strip(): v.strip() for k, v in (p.split('=') for p in userinfo.split(';') if '=' in p)}
                if "expire" in parts:
                    try:
                        expire_ts = int(parts["expire"])
                        if expire_ts > 0 and expire_ts < int(time.time()):
                            add_log(sub_id, "EXPIRED_ACCESS", f"Süresi doldu. Expire: {expire_ts}", hardware_id=hw, ip=ip, ua=ua)
                            exp_msg = get_global("expired_metadata", "Abonelik süreniz dolmuştur.")
                            out_headers = {h: resp.headers.get(h) for h in ["subscription-userinfo","profile-update-interval","profile-title","profile-web-page-url","support-url"] if resp.headers.get(h)}
                            return Response(exp_msg, media_type="text/plain; charset=utf-8", headers=out_headers)
                    except: pass
                try:
                    total_bytes = int(parts.get("total", 0))
                    used_bytes = int(parts.get("upload", 0)) + int(parts.get("download", 0))
                    # Cok-panel: kullanicinin TUM panellerdeki toplam trafigi varsa onu kullan
                    _pt = _panel_user_total(_sub_username(sub)) if sub else None
                    if _pt is not None and _pt > used_bytes: used_bytes = _pt
                    if total_bytes > 0:
                        if used_bytes >= total_bytes:
                            add_log(sub_id, "QUOTA_EXCEEDED", f"Kota aşıldı! Harcanan: {round(used_bytes/1073741824, 2)}GB / Kota: {round(total_bytes/1073741824, 2)}GB", hardware_id=hw, ip=ip, ua=ua)
                            data_msg = get_global("data_limit_metadata", "Abonelik veri kotanız (GB) dolmuştur!")
                            out_headers = {h: resp.headers.get(h) for h in ["subscription-userinfo","profile-update-interval","profile-title","profile-web-page-url","support-url"] if resp.headers.get(h)}
                            return Response(data_msg, media_type="text/plain; charset=utf-8", headers=out_headers)
                except: pass

            user_data = {
                "username": sub["name"] if sub else "Bilinmeyen",
                "used": f"{used_bytes / 1073741824:.2f}",
                "limit": f"{total_bytes / 1073741824:.2f}" if total_bytes > 0 else "Limitsiz"
            }
            if expire_ts > 0:
                user_data["expiretime"] = time.strftime('%d.%m.%Y', time.localtime(expire_ts))
                rem_days = (expire_ts - int(time.time())) / 86400
                user_data["remaing"] = f"{int(rem_days)} gün" if rem_days > 0 else "Bitti"
                user_data["remaining"] = user_data["remaing"]
            else:
                user_data["expiretime"] = "Sınırsız"
                user_data["remaing"] = "Sınırsız"
                user_data["remaining"] = "Sınırsız"

            content, was_base64 = decode_sub_content(resp.content)
            
            raw_meta = ""
            if sub["metadata"]: raw_meta += sub["metadata"].rstrip("\n") + "\n"
            if gm := get_global("metadata",""): raw_meta += gm.rstrip("\n") + "\n"

            prefix = ""
            if raw_meta:
                for k, v in user_data.items():
                    raw_meta = raw_meta.replace(f"{{{k}}}", str(v))
                
                prefix_lines = []
                current_tag = ""
                current_is_base64 = False
                current_buffer = []

                def flush_buffer():
                    if not current_buffer and not current_tag: return ""
                    text = "\n".join(current_buffer)
                    if current_is_base64:
                        b64_str = base64.b64encode(text.encode('utf-8')).decode('utf-8')
                        return f"{current_tag}{b64_str}"
                    else:
                        return f"{current_tag}{text}"

                for line in raw_meta.split("\n"):
                    line = line.strip('\r')
                    if not line.strip(): continue
                    m = re.match(r'^(#[a-zA-Z0-9_-]+:)\s*(.*)$', line)
                    if m:
                        if current_buffer or current_tag:
                            res = flush_buffer()
                            if res: prefix_lines.append(res)
                        tag_part = m.group(1)
                        val_part = m.group(2)
                        if val_part.lower().startswith("base64:"):
                            current_is_base64 = True
                            val_part = val_part[7:].lstrip()
                            current_tag = f"{tag_part} base64:"
                        else:
                            current_is_base64 = False
                            current_tag = f"{tag_part} "
                        current_buffer = [val_part] if val_part else []
                    else:
                        current_buffer.append(line)

                if current_buffer or current_tag:
                    res = flush_buffer()
                    if res: prefix_lines.append(res)
                    
                prefix = "\n".join(prefix_lines) + "\n" if prefix_lines else ""

            extra_pos = get_global("extra_position","top")
            manual_lines = [l.strip() for l in get_global("extra_content","").split("\n") if l.strip()]

            try: ext_cache = json.loads(get_global("external_fetched_cache", "{}"))
            except: ext_cache = {}
            try: subs_data = json.loads(get_global("external_subs_data", "[]"))
            except: subs_data = []

            header_lines = [l for l in content.split("\n") if l.strip().startswith("#") and "://" not in l]

            auto_ren = get_global("auto_rename_enabled", "1") == "1"
            remote_lines = []  # KAYNAK SUB'in kendi node'lari (alexander77'den gelen)
            remote_raw = []  # kaynak node'lar HAM (rename slot/global ayarina gore uygulanir)
            for l in content.split("\n"):
                lt = l.strip()
                if lt and not (lt.startswith("#") and "://" not in lt):
                    remote_raw.append(lt)
            # Coklu panel birlestirme: ayni kullanicinin ikincil (3x-ui) panellerdeki config'leri
            # Panel slotu (🔗) olan paneller kendi blogunda durur; slotu olmayanlar kaynak node'lara katilir.
            try: _extra_by = await _secondary_links_by_panel(_sub_username(sub))
            except Exception as e:
                print(f"[MERGE] {e!r}"); _extra_by = {}
            _panel_slotted = {it.get("panel") for it in subs_data if it.get("panel")}
            _seen = set(remote_raw)
            for _pk, _links in _extra_by.items():
                if _pk in _panel_slotted: continue
                for x in _links:
                    if x not in _seen: _seen.add(x); remote_raw.append(x)

            sort_en = get_global("enable_sort", "1") == "1"
            o, v, m = _eff_orders(None)

            # Slotlari SURUKLEME SIRASINA gore diz: harici slotlar + "📍 Kaynak Sub" bloğu.
            top_seq, bot_seq = [], []
            source_slot_used = False
            for i, item in enumerate(subs_data):
                pos = item.get("pos", "bottom")
                tgt = top_seq if pos == "top" else bot_seq
                if item.get("source") or item.get("panel"):  # 📍 KAYNAK SUB / 🔗 PANEL bloğu — konum + isim kullanici secer
                    if item.get("source"): source_slot_used = True
                    protos = item.get("protocols") or []
                    pool = remote_raw if item.get("source") else _extra_by.get(item.get("panel"), [])
                    src = [l for l in pool if (not protos or _node_allowed(l, protos))]
                    if item.get("rename", True) is not False:
                        src = [auto_rename_node(x) for x in src]
                    if sort_en:
                        so, sv, sm = _eff_orders(_slot_sort_from(item))
                        src = sort_links(src, so, sv, sm)
                    tgt.extend(src)
                else:
                    url = (item.get("url", "") or "").strip()
                    key = url if url else ("local:" + str(item.get("sid") or i))
                    cd = ext_cache.get(key, {})
                    tgt.extend(cd.get("top", []) + cd.get("bottom", []))

            if extra_pos == "top": top_seq += manual_lines
            else: bot_seq += manual_lines

            if source_slot_used:
                sorted_lines = top_seq + bot_seq
            else:
                # Kaynak bloğu yok: global auto_rename ile isimlendir, ortaya koy
                remote_lines = [auto_rename_node(x) if auto_ren else x for x in remote_raw]
                if sort_en: remote_lines = sort_links(remote_lines, o, v, m)
                sorted_lines = top_seq + remote_lines + bot_seq

            out_headers = {h: resp.headers.get(h) for h in ["subscription-userinfo","profile-update-interval","profile-title","profile-web-page-url","support-url"] if resp.headers.get(h)}

            # Istemci profili: UA eslesirse sub'i o uygulamanin istedigi formatta ( or. JSON) ver
            prof = _match_client_profile(ua)
            if prof:
                rendered, ctype = _emit_profile(prof, sorted_lines, user_data)
                update_traffic(sub_id, len(rendered.encode()))
                return Response(rendered, media_type=ctype, headers=out_headers)
            # JSON alan uygulamalar (UA eslesirse xray-json ver)
            if _ua_wants_json(ua):
                rendered = _emit_xrayjson(sorted_lines)
                update_traffic(sub_id, len(rendered.encode()))
                json_headers = dict(out_headers); json_headers.update(_directives_to_headers(prefix))
                return Response(rendered, media_type="application/json; charset=utf-8", headers=json_headers)

            final_content = prefix + "\n".join(header_lines + sorted_lines)

            if was_base64:
                final_content = base64.b64encode(final_content.encode('utf-8')).decode('utf-8')

            update_traffic(sub_id,len(final_content.encode()))
            return Response(final_content, media_type="text/plain; charset=utf-8", headers=out_headers)
    except Exception as e:
        add_log(sub_id,"FETCH_ERROR",str(e)[:200],hardware_id=hw,ip=ip,ua=ua)
        return PlainTextResponse(get_global("proxy_error_message", "Sunucu gecici olarak kullanilamiyor."), status_code=200)

@app.post("/panel/api/format-links")
async def api_format_links(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    lines = d.get("lines", "").split("\n")
    res = [auto_rename_node(l) if "://" in l else l for l in lines]
    return {"ok": True, "formatted": "\n".join(res)}

async def _get_source_lines(url, local, opts=None):
    """URL varsa (slot UA/HWID ile) cek — hata/bossa fallback'e gec; yoksa local metni cozumle."""
    if url:
        try: return await _fetch_sub_lines(url, opts)
        except Exception:
            fb = ((opts or {}).get("fallback") or "").strip()
            if not fb: raise
            lines = await _fetch_sub_lines(fb, opts)
            opts["_used"] = "fallback"
            return lines
    content, _ = decode_sub_content((local or "").encode("utf-8"))
    return content.split("\n")

@app.post("/panel/api/external/preview")
async def api_preview_slot(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    url = d.get("url", "").strip()
    local = (d.get("local", "") or "")
    allowed_protos = d.get("protocols", [])
    if not url and not local.strip(): return {"ok": False, "msg": "URL veya local içerik gerekli."}
    try:
        lines = await _get_source_lines(url, local, d)
    except Exception as e:
        return {"ok": False, "msg": f"Bağlantı hatası: {str(e)[:50]}"}
    auto_ren = get_global("auto_rename_enabled", "1") == "1"
    names, total = [], 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "://" not in line: continue
        if not _node_allowed(line, allowed_protos): continue
        total += 1
        if len(names) < 100:
            rn = auto_rename_node(line) if auto_ren else line
            names.append(_display_name(rn))
    return {"ok": True, "total": total, "names": names, "renamed": auto_ren}

@app.get("/panel/api/config/export")
async def config_export(request: Request):
    if not authed(request): raise HTTPException(401)
    with db_session() as con:
        rows = con.execute("SELECT key,value FROM global_settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows if r["key"] != "external_fetched_cache"}
    return {"ok": True, "version": 1, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": settings}

@app.post("/panel/api/config/import")
async def config_import(request: Request):
    if not authed(request): raise HTTPException(401)
    try: d = await request.json()
    except: return {"ok": False, "msg": "Geçersiz JSON"}
    settings = d.get("settings") if isinstance(d, dict) else None
    if not isinstance(settings, dict) or not settings:
        return {"ok": False, "msg": "JSON içinde 'settings' nesnesi bulunamadı."}
    cnt = 0
    for k, v in settings.items():
        if k == "external_fetched_cache": continue
        set_global(str(k), "" if v is None else str(v)); cnt += 1
    global _last_external_fetch_time; _last_external_fetch_time = 0
    add_panel_log("CONFIG_IMPORT", f"{cnt} ayar içe aktarıldı", get_ip(request))
    return {"ok": True, "count": cnt}

@app.post("/panel/api/external/preview-final")
async def api_preview_final(request: Request):
    """Tum kaynaklar (URL + local) + manuel linkler birlestirilip, mevcut siralama/rename
    ayarlari uygulanmis FINAL sub ciktisini dondurur (musterinin kendi node'lari haric).
    Ping ATILMAZ — amac sira/isim onizlemesi, hiz icin."""
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    slots = d.get("slots", [])
    extra_position = d.get("extra_position", "top")
    extra_content = d.get("extra_content", "") or ""
    enable_sort = bool(d.get("enable_sort", True))
    auto_ren = bool(d.get("auto_rename", True))
    sort_order = [x for x in (d.get("sort_order") or "").split(",") if x] or PROTOCOLS
    vless_sort = [x for x in (d.get("vless_sort") or "").split(",") if x] or VLESS_SUBTYPES
    vmess_sort = [x for x in (d.get("vmess_sort") or "").split(",") if x] or VMESS_SUBTYPES

    cdn_on = get_global("cdn_label_enabled", "0") == "1"
    vps_provs = _vps_enabled_providers()
    top, bot = [], []
    errors = 0
    for s in slots:
        url = (s.get("url", "") or "").strip()
        local = (s.get("local", "") or "")
        protos = s.get("protocols", [])
        pos = s.get("pos", "bottom")
        if s.get("source") or s.get("panel"):  # abone bazli bloklar: konumu yer tutucu ile goster
            ph = "📍 [Kaynak Sub — abonenin kendi kodları]" if s.get("source") else f"🔗 [{s.get('pname') or s.get('panel')} — abonenin bu paneldeki kodları]"
            (top if pos == "top" else bot).append("\x00PH" + ph)
            continue
        if not url and not local.strip(): continue
        try:
            lines = await _get_source_lines(url, local, s)
        except:
            errors += 1
            continue
        slot_nodes = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "://" not in line: continue
            if not _node_allowed(line, protos): continue
            if auto_ren:
                rn = auto_rename_node(line, cdn_label=await _cdn_label_for(line, cdn_on), vps_label=await _vps_label_for(line, vps_provs))
            else:
                rn = line
            slot_nodes.append(rn)
        # Her slot kendi (yoksa gonderilen global) protokol sirasina gore dizilir
        if enable_sort:
            so = [x for x in (s.get("sort_order") or "").split(",") if x] or sort_order
            sv = [x for x in (s.get("vless_sort") or "").split(",") if x] or vless_sort
            sm = [x for x in (s.get("vmess_sort") or "").split(",") if x] or vmess_sort
            slot_nodes = sort_links(slot_nodes, so, sv, sm)
        (top if pos == "top" else bot).extend(slot_nodes)

    manual = [l.strip() for l in extra_content.split("\n") if l.strip()]
    manual = [(auto_rename_node(l) if (auto_ren and "://" in l) else l) for l in manual]
    if extra_position == "top": top = top + manual
    else: bot = bot + manual

    # Dis kaynaklar slot-bazli sirali; global re-sort YOK (slot sirasi korunur)
    all_lines = top + bot
    names = [(l[3:] if l.startswith("\x00PH") else _display_name(l)) for l in all_lines]
    return {"ok": True, "total": sum(1 for l in all_lines if not l.startswith("\x00PH")), "names": names[:300], "errors": errors}

@app.post("/panel/api/external/test-slot")
async def api_test_slot(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    url = d.get("url", "").strip()
    local = (d.get("local", "") or "")
    allowed_protos = d.get("protocols", [])
    do_ping = d.get("ping", False)

    if not url and not local.strip(): return {"ok": False, "msg": "Lütfen URL girin veya local içerik yapıştırın."}
    timeout = float(get_global("ping_timeout", "2.5"))
    xray_status = "🟢 Xray Motoru Aktif" if os.path.exists(os.path.join(BASE_DIR, "xray", "xray")) else "🔴 Xray Bulunamadı!"

    try:
        lines = await _get_source_lines(url, local, d)
    except Exception as e:
        return {"ok": False, "msg": f"Bağlantı hatası: {str(e)[:50]}"}

    nodes_to_test = []
    for line in lines:
        try:
            line = line.strip()
            if not line or line.startswith("#") or "://" not in line: continue
            if _node_allowed(line, allowed_protos):
                outb = parse_link_for_xray(line)
                if outb: nodes_to_test.append((line, outb))
        except: pass

    if not nodes_to_test:
        return {"ok": True, "msg": "Test edilecek uyumlu proxy bulunamadı."}

    if do_ping:
        api_online = False

        sem = asyncio.Semaphore(4)
        max_ms = _ping_max_ms(d)
        async def test_node_for_api(line, outb):
            try:
                async with sem:
                    return line, await _node_latency(outb, timeout, api_online)
            except Exception: return line, None
        results = await asyncio.gather(*[test_node_for_api(l, o) for l, o in nodes_to_test], return_exceptions=True)
        lats = [r[1] for r in results if not isinstance(r, Exception) and r[1] is not None]
        ok_n = sum(1 for x in lats if not max_ms or x <= max_ms)
        slow = len(lats) - ok_n
        dead = len(nodes_to_test) - len(lats)
        avg = f" | ⏱ ort. {int(sum(lats)/len(lats))} ms" if lats else ""
        lim = f" | 🐢 {max_ms} ms üstü (alınmaz): {slow}" if max_ms else ""
        fbm = " | ↪ ANA SUB AÇILMADI → FALLBACK" if d.get("_used") == "fallback" else ""
        if d.get("fallback") and not d.get("_used") and ok_n == 0 and nodes_to_test:
            try:
                fl = await _fetch_sub_lines(d["fallback"].strip(), d)
                fbm = f" | ↪ Ana sub'da çalışan yok → fallback devreye girer ({len(fl)} kod)"
            except Exception as e: fbm = f" | ⚠ Fallback da açılmadı: {str(e)[:40]}"
        return {"ok": True, "msg": f"{xray_status} | 📦 Filtrelenen: {len(nodes_to_test)} | ✅ Alınacak: {ok_n}{lim} | ❌ Ölü: {dead}{avg}{fbm}"}
    else:
        return {"ok": True, "msg": f"⚙️ Ping Kapalı! | 📦 Filtreden Geçen ve Alınan: {len(nodes_to_test)}" + (" | ↪ ANA SUB AÇILMADI → FALLBACK" if d.get("_used") == "fallback" else "")}

@app.post("/panel/api/external/slot-status")
async def api_slot_status(request: Request):
    """Her URL slotunun son cekimde ana/fallback/basarisiz durumu."""
    if not authed(request): raise HTTPException(401)
    return {"ok": True, "status": _slot_status}

@app.post("/panel/api/external/force-fetch-single")
async def force_fetch_external_single(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    url = d.get("url", "").strip()
    local = (d.get("local", "") or "")
    pos = d.get("pos", "bottom")
    allowed_protos = d.get("protocols", [])
    do_ping = d.get("ping", False)

    if not url and not local.strip(): return {"ok": False, "msg": "URL veya local içerik gerekli"}
    ping_timeout = float(get_global("ping_timeout", "2.5"))
    key = url if url else ("local:" + str(d.get("sid") or "manual"))
    slot_sort = _slot_sort_from(d)
    rename = d.get("rename", True) is not False

    try:
        if url:
            url_top, url_bot = await _fetch_and_process_url(url, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, _ping_max_ms(d), d)
        else:
            url_top, url_bot = await _process_local_source(local, pos, allowed_protos, do_ping, ping_timeout, slot_sort, rename, _ping_max_ms(d))
    except Exception as e:
        return {"ok": False, "msg": f"Bağlantı hatası: {str(e)[:50]}"}

    try: cache = json.loads(get_global("external_fetched_cache", "{}"))
    except: cache = {}
    cache[key] = {"top": url_top, "bottom": url_bot}
    set_global("external_fetched_cache", json.dumps(cache))

    return {"ok": True, "count": len(url_top) + len(url_bot)}

@app.post("/panel/api/panels/test")
async def panels_test(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    if d.get("auto"):  # otomatik ana panel: kimlik bilgileri .env'den
        d = {"type": "marzban", "url": MARZBAN_URL, "user": MARZBAN_USER, "pass": MARZBAN_PASS}
    url = (d.get("url", "") or "").strip()
    if not url: return {"ok": False, "msg": "URL gerekli"}
    ok, msg, n = await _panel_test(d.get("type", "marzban"), url, d.get("user", ""), d.get("pass", ""), d.get("token", ""))
    out = {"ok": ok, "msg": msg, "users": n}
    if ok and d.get("type") == "3xui" and d.get("token"):
        try:
            owned, total = await _macros_owned_clients({"url": url, "token": d.get("token")})
            out.update(users=total, macros=len(owned), own=total - len(owned))
        except Exception: pass
    return out

@app.post("/panel/api/panels/sync")
async def panels_sync(request: Request):
    if not authed(request): raise HTTPException(401)
    res = await _sync_panels()
    add_panel_log("PANEL_SYNC", f"Manuel senkron: {res}", get_ip(request))
    return res

@app.post("/panel/api/panels/sync-status")
async def panels_sync_status(request: Request):
    """Tum entegre panellerin durumunu + toplam kullanici sayilarini (salt-okunur) dondurur."""
    if not authed(request): raise HTTPException(401)
    out = []
    for p in _effective_panels():
        if not p.get("enabled", True):
            out.append({"name": p.get("name", ""), "type": p.get("type"), "ok": None, "msg": "Devre dışı", "users": 0}); continue
        ok, msg, n = await _panel_test(p.get("type", "marzban"), p.get("url", ""), p.get("user", ""), p.get("pass", ""), p.get("token", ""))
        out.append({"name": p.get("name", ""), "type": p.get("type"), "is_main": p.get("is_main", False), "ok": ok, "msg": msg, "users": n})
    return {"ok": True, "panels": out}

def _saved_panel_by_pkey(pkey):
    try: lst = json.loads(get_global("integrated_panels", "[]"))
    except Exception: lst = []
    for i, p in enumerate(lst if isinstance(lst, list) else []):
        if _pkey(p) == pkey: return lst, i, p
    return lst, -1, None

async def _macros_owned_clients(p):
    """Bu 3x-ui panelinde MACROS'UN ACTIGI client'lar: group=='macros' VE subId == macros'un email'den
    urettigi sabit uuid5 — ikisi birden. Panelin kendi (onceden var olan) kullanicilari asla secilmez."""
    async with _XuiConn(p) as conn:
        cl = await _xui_clients(conn)
    if cl is None: raise RuntimeError("3x-ui client listesi okunamadı")
    owned = [c["email"] for c in cl if c.get("email") and c.get("group") == "macros"
             and c.get("subId") == _macros_subid(c["email"])]
    return owned, len(cl)

@app.post("/panel/api/panels/remove")
async def panels_remove(request: Request):
    """Entegre paneli kaldirir. Once macros'un o panelde actigi kullanicilari siler (bulkDel), hepsi
    silinince paneli + eslesmeleri + o panele ait slotlari kaldirir. dry=true -> sadece sayim.
    Silme yarim kalirsa panel KALDIRILMAZ (tekrar denenebilir)."""
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    pkey, dry, keep_users = d.get("pkey", ""), bool(d.get("dry")), bool(d.get("keep_users"))
    lst, idx, p = _saved_panel_by_pkey(pkey)
    if p is None: return {"ok": False, "msg": "Panel bulunamadı (kaydedilmemiş olabilir)"}
    if pkey == _pkey({"url": MARZBAN_URL or ""}) and MARZBAN_URL: return {"ok": False, "msg": "Ana panel kaldırılamaz"}
    owned, total = [], 0
    if p.get("type") == "3xui" and p.get("token"):
        try: owned, total = await _macros_owned_clients(p)
        except Exception as e:
            if not keep_users: return {"ok": False, "msg": f"Panele bağlanılamadı: {e} — kullanıcılar silinemez. 'Kullanıcıları bırak' ile yine de kaldırabilirsin."}
    if dry:
        return {"ok": True, "dry": True, "owned": len(owned), "others": max(0, total - len(owned)), "name": p.get("name") or pkey}
    deleted, errors = 0, []
    async with _sync_lock:  # senkron araya girip yeniden olusturmasin
        if owned and not keep_users:
            async with _XuiConn(p) as conn:
                for i in range(0, len(owned), 100):
                    part = owned[i:i + 100]
                    j = await conn.call("POST", "/panel/api/clients/bulkDel", json={"emails": part, "keepTraffic": False})
                    if j.get("success"):
                        o = j.get("obj") or {}
                        deleted += int(o.get("deleted", len(part)) or 0)
                        errors += [f"{x.get('email')}: {x.get('reason')}" for x in (o.get("skipped") or []) if x.get("reason") != "client not found"]
                    else:
                        errors.append(str(j.get("msg") or "bulkDel başarısız")[:200])
        if errors:
            add_panel_log("PANEL_REMOVE_FAIL", f"{p.get('name') or pkey}: {deleted} silindi, hata: {'; '.join(errors[:10])}", get_ip(request))
            return {"ok": False, "msg": f"{deleted} kullanıcı silindi ama {len(errors)} hata var — panel kaldırılmadı, tekrar dene.", "errors": errors[:20], "deleted": deleted}
        lst.pop(idx); set_global("integrated_panels", json.dumps(lst))
        try: sd = json.loads(get_global("external_subs_data", "[]"))
        except Exception: sd = []
        if isinstance(sd, list) and any(x.get("panel") == pkey for x in sd):
            set_global("external_subs_data", json.dumps([x for x in sd if x.get("panel") != pkey]))
        with DB_LOCK, db_session() as con:
            con.execute("DELETE FROM panel_links WHERE pkey=?", (pkey,))
        for k in [k for k in _sublinks_cache if k[0] == pkey]: _sublinks_cache.pop(k, None)
    add_panel_log("PANEL_REMOVE", f"{p.get('name') or pkey}: panel kaldırıldı, macros kullanıcısı silindi: {deleted}" + (" (kullanıcılar bırakıldı)" if keep_users else ""), get_ip(request))
    return {"ok": True, "deleted": deleted, "name": p.get("name") or pkey}

@app.post("/panel/api/global/clean-db")
async def clean_database(request: Request):
    if not authed(request): raise HTTPException(401)
    with DB_LOCK, db_session() as con:
        try:
            con.execute("DELETE FROM subs")
            con.execute("DELETE FROM devices")
            con.execute("DELETE FROM logs")
            con.execute("DELETE FROM traffic")
            con.commit()
        except: pass
    return {"ok":True}

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request): return templates.TemplateResponse(request=request, name="login.html", context={"error":""})

@app.post("/panel/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str=Form(...), password: str=Form(...)):
    ip = get_ip(request)
    if username==ADMIN_USER and password==ADMIN_PASS:
        sid = secrets.token_hex(32); _session_put(sid, username); add_panel_log("LOGIN",f"Giris: {username}",ip)
        r = RedirectResponse("/panel/dashboard",status_code=303); r.set_cookie("sid",sid,httponly=True,samesite="lax",max_age=86400*7); return r
    return templates.TemplateResponse(request=request, name="login.html", context={"error":"Hatalı giriş"})

@app.get("/panel/logout")
async def logout(request: Request):
    if sid := request.cookies.get("sid"): _session_del(sid)
    r = RedirectResponse("/panel/login"); r.delete_cookie("sid"); return r

@app.get("/panel")
async def panel_root(): return RedirectResponse("/panel/dashboard")

@app.get("/panel/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, q: str=""):
    if not authed(request): return RedirectResponse("/panel/login")
    s = f"%{q}%"
    with db_session() as con:
        subs = con.execute("SELECT s.*,COUNT(DISTINCT d.id) as device_count,COALESCE(t.bytes_down,0) as bytes_down,COALESCE(t.req_count,0) as req_count FROM subs s LEFT JOIN devices d ON d.sub_id=s.id LEFT JOIN traffic t ON t.sub_id=s.id WHERE s.name LIKE ? OR s.url LIKE ? OR s.note LIKE ? OR s.last_check_username LIKE ? GROUP BY s.id ORDER BY s.created_at DESC",(s,s,s,s)).fetchall()
        ua_blacklist = con.execute("SELECT * FROM ua_blacklist ORDER BY id DESC").fetchall()
        ua_whitelist = con.execute("SELECT * FROM ua_whitelist ORDER BY id DESC").fetchall()
        stats = con.execute("SELECT (SELECT COUNT(*) FROM subs) as total_subs, (SELECT COUNT(*) FROM devices) as total_devices, (SELECT COALESCE(SUM(req_count),0) FROM traffic) as total_reqs, (SELECT COUNT(*) FROM logs WHERE event='UA_BLACKLISTED') as ua_blocks, (SELECT COUNT(*) FROM logs WHERE event='DEVICE_LIMIT_EXCEEDED') as dev_blocks, (SELECT COUNT(*) FROM subs WHERE blocked=1) as blocked_subs").fetchone()

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "subs":[dict(s) for s in subs],
        "ua_blacklist":[dict(u) for u in ua_blacklist], "ua_whitelist":[dict(u) for u in ua_whitelist], "stats":dict(stats), "q":q,
        "global_metadata":get_global("metadata",""), "global_blocked_metadata":get_global("blocked_metadata",""),
        "global_blocked_device_metadata":get_global("blocked_device_metadata",""), 
        "global_expired_metadata":get_global("expired_metadata", "Abonelik süreniz dolmuştur!"),
        "global_data_limit_metadata":get_global("data_limit_metadata", "Abonelik veri kotanız (GB) dolmuştur!"),
        "global_limit_enabled": get_global("device_limit_enabled", "1") == "1",
        "default_device_limit": get_global("default_device_limit", "1"),
        "sys_info": get_sys_info()
    })

@app.get("/panel/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if not authed(request): return RedirectResponse("/panel/login")
    with db_session() as con:
        sys_logs = con.execute("SELECT l.*, s.name as sub_name FROM logs l LEFT JOIN subs s ON s.id=l.sub_id ORDER BY l.ts DESC LIMIT 300").fetchall()
        panel_logs = con.execute("SELECT * FROM panel_logs ORDER BY ts DESC LIMIT 100").fetchall()
    return templates.TemplateResponse(request=request, name="logs.html", context={"sys_logs": [dict(x) for x in sys_logs], "panel_logs": [dict(x) for x in panel_logs]})

@app.get("/panel/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    if not authed(request): return RedirectResponse("/panel/login")
    with db_session() as con:
        models = con.execute("SELECT device_model, COUNT(*) as c FROM devices WHERE device_model != '' GROUP BY device_model ORDER BY c DESC LIMIT 15").fetchall()
        apps = con.execute("SELECT app_name, COUNT(*) as c FROM devices WHERE app_name != '' GROUP BY app_name ORDER BY c DESC LIMIT 15").fetchall()
        exceeded = con.execute("SELECT s.name, s.url, COUNT(l.id) as exc_count FROM logs l JOIN subs s ON s.id=l.sub_id WHERE l.event='DEVICE_LIMIT_EXCEEDED' GROUP BY l.sub_id ORDER BY exc_count DESC LIMIT 20").fetchall()
    return templates.TemplateResponse(request=request, name="stats.html", context={"models": [dict(x) for x in models], "apps": [dict(x) for x in apps], "exceeded": [dict(x) for x in exceeded]})

@app.post("/panel/api/global/update")
async def update_global(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json(); ip = get_ip(request)
    set_global("metadata",d.get("metadata","")); set_global("blocked_metadata",d.get("blocked_metadata","")); set_global("blocked_device_metadata",d.get("blocked_device_metadata",""))
    set_global("expired_metadata",d.get("expired_metadata",""))
    set_global("data_limit_metadata",d.get("data_limit_metadata",""))
    set_global("default_device_limit", str(d.get("default_device_limit", 1)))
    add_panel_log("GLOBAL_UPDATE","Ayarlar güncellendi",ip)
    return {"ok":True}

@app.post("/panel/api/global/device-limit-toggle")
async def toggle_global_device_limit(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json(); val = "1" if d.get("enabled", True) else "0"
    set_global("device_limit_enabled", val); return {"ok": True}

@app.post("/panel/api/ua-blacklist/add")
async def add_ua_blacklist(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO ua_blacklist(pattern,note) VALUES(?,?)",(d.get("pattern",""),d.get("note",""))); con.commit()
        except: pass
    return {"ok":True}

@app.delete("/panel/api/ua-blacklist/{item_id}")
async def del_ua_blacklist(item_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with DB_LOCK, db_session() as con:
        try: con.execute("DELETE FROM ua_blacklist WHERE id=?",(item_id,)); con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/ua-whitelist/add")
async def add_ua_whitelist(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    with DB_LOCK, db_session() as con:
        try: con.execute("INSERT INTO ua_whitelist(pattern,note) VALUES(?,?)",(d.get("pattern",""),d.get("note",""))); con.commit()
        except: pass
    return {"ok":True}

@app.delete("/panel/api/ua-whitelist/{item_id}")
async def del_ua_whitelist(item_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with DB_LOCK, db_session() as con:
        try: con.execute("DELETE FROM ua_whitelist WHERE id=?",(item_id,)); con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/sub/check")
async def check_sub(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json(); url, sub_id, username, userinfo = d.get("url",""), d.get("id"), "", ""
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent":"clash/check"})
            status, pt = f"OK {resp.status_code}", resp.headers.get("profile-title","")
            if pt.startswith("base64:"):
                try: pt = base64.b64decode(pt[7:]).decode().strip()
                except: pt = ""
            username, userinfo = extract_filename(resp.headers) or pt or "", resp.headers.get("subscription-userinfo","")
            with DB_LOCK, db_session() as con:
                try:
                    if username: con.execute("UPDATE subs SET name=?,last_checked=datetime('now'),last_check_status=?,last_check_username=? WHERE id=?",(username,status,username,sub_id))
                    else: con.execute("UPDATE subs SET last_checked=datetime('now'),last_check_status=? WHERE id=?",(status,sub_id))
                    con.commit()
                except: pass
    except Exception as e:
        status = f"HATA: {str(e)[:80]}"
        with DB_LOCK, db_session() as con:
            try: con.execute("UPDATE subs SET last_check_status=? WHERE id=?",(status,sub_id)); con.commit()
            except: pass
    return {"status":status,"username":username,"userinfo":userinfo}

@app.post("/panel/api/sub/update")
async def update_sub(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json()
    device_limit = int(d.get("device_limit",1))
    force_limit = 1 if d.get("force_limit", False) else 0
    with DB_LOCK, db_session() as con:
        try: con.execute("UPDATE subs SET device_limit=?,unlimited=?,force_limit=?,note=?,metadata=?,blocked=?,blocked_metadata=?,blocked_device_metadata=? WHERE id=?",(device_limit, 1 if device_limit <= 0 else 0, force_limit, d.get("note",""), d.get("metadata",""),d.get("blocked",0),d.get("blocked_metadata",""),d.get("blocked_device_metadata",""),d.get("id"))); con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/sub/reset-devices")
async def reset_devices(request: Request):
    if not authed(request): raise HTTPException(401)
    sid = (await request.json()).get("id")
    with DB_LOCK, db_session() as con:
        try: con.execute("DELETE FROM devices WHERE sub_id=?",(sid,)); con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/device/delete")
async def delete_device(request: Request):
    if not authed(request): raise HTTPException(401)
    did = (await request.json()).get("id")
    with DB_LOCK, db_session() as con:
        try: con.execute("DELETE FROM devices WHERE id=?",(did,)); con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/device/block")
async def block_device(request: Request):
    if not authed(request): raise HTTPException(401)
    d = await request.json(); state, did = d.get("blocked",1), d.get("id")
    with DB_LOCK, db_session() as con:
        try: con.execute("UPDATE devices SET blocked=? WHERE id=?",(state,did)); con.commit()
        except: pass
    return {"ok":True}

@app.get("/panel/api/sub/{sub_id}/devices")
async def get_devices(sub_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with db_session() as con: r = con.execute("SELECT * FROM devices WHERE sub_id=? ORDER BY first_seen DESC",(sub_id,)).fetchall()
    return [dict(x) for x in r]

@app.get("/panel/api/sub/{sub_id}/logs")
async def get_sub_logs(sub_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with db_session() as con: r = con.execute("SELECT * FROM logs WHERE sub_id=? ORDER BY ts DESC LIMIT 100",(sub_id,)).fetchall()
    return [dict(x) for x in r]

@app.get("/panel/api/sub/{sub_id}/history")
async def get_sub_history(sub_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with db_session() as con: r = con.execute("SELECT * FROM panel_logs WHERE detail LIKE ? ORDER BY ts DESC LIMIT 50",(f"%Sub ID:{sub_id}%",)).fetchall()
    return [dict(x) for x in r]

@app.delete("/panel/api/sub/{sub_id}")
async def delete_sub(sub_id: int, request: Request):
    if not authed(request): raise HTTPException(401)
    with DB_LOCK, db_session() as con:
        try:
            for t in ["devices","traffic","logs","subs"]: con.execute(f"DELETE FROM {t} WHERE {'id' if t=='subs' else 'sub_id'}=?",(sub_id,))
            con.commit()
        except: pass
    return {"ok":True}

@app.post("/panel/api/external/force-fetch")
async def force_fetch_external(request: Request):
    if not authed(request): raise HTTPException(401)
    try:
        data = await do_external_fetch()
        return {"ok": True, "count": data["total"], "details": data["details"]}
    except Exception as e: return {"ok": False, "msg": str(e)}

@app.get("/panel/sort-settings", response_class=HTMLResponse)
async def sort_settings_get(request: Request):
    if not authed(request): return RedirectResponse("/panel/login")
    from jinja2 import Template
    try: ext_subs = json.loads(get_global("external_subs_data", "[]"))
    except: ext_subs = []
    if not ext_subs: ext_subs = [{"url": "", "pos": "bottom", "note": "", "ping": False}]
    return HTMLResponse(templates.env.get_template("sort-settings.html").render(
        enable_sort=get_global("enable_sort", "1"),
        auto_rename_enabled=get_global("auto_rename_enabled", "1"),
        allowed_panels=get_global("allowed_panels", ""),
        sort_order=get_global("sort_order", ",".join(PROTOCOLS)), 
        vless_sort=get_global("vless_sort", ",".join(VLESS_SUBTYPES)),
        vmess_sort=get_global("vmess_sort", ",".join(VMESS_SUBTYPES)), 
        extra_position=get_global("extra_position", "top"),
        extra_content=get_global("extra_content", ""), 
        proxy_error_message=get_global("proxy_error_message", "Sunucu gecici olarak kullanilamiyor."),
        source_404_message=get_global("source_404_message", "Kaynak abonelik bulunamadi (404)."), 
        external_sub_interval=get_global("external_sub_interval", "60"),
        ping_timeout=get_global("ping_timeout", "2.5"),
        ping_max_ms=get_global("ping_max_ms", "0"),
        cdn_label_enabled=get_global("cdn_label_enabled", "0"),
        vps_label_enabled=get_global("vps_label_enabled", "0"),
        vps_providers=get_global("vps_providers", "azure,amazon,google,oracle"),
        cdn_labels=_cdn_label_map(),
        vps_labels=_vps_label_map(),
        client_profiles=_client_profiles(),
        json_apps=get_global("json_apps", ""),
        json_direct_domains=get_global("json_direct_domains", ""),
        json_direct_ips=get_global("json_direct_ips", ""),
        json_block_ads=get_global("json_block_ads", "0"),
        json_balancer=get_global("json_balancer", "0"),
        json_balancer_ext=get_global("json_balancer_ext", "1"),
        json_balancer_name=get_global("json_balancer_name", "⚡ Auto (fastest)"),
        json_balancer_strategy=get_global("json_balancer_strategy", "leastPing"),
        json_balancer_pos=get_global("json_balancer_pos", "first"),
        json_balancer_interval=get_global("json_balancer_interval", "1m"),
        json_balancer_url=get_global("json_balancer_url", "https://www.gstatic.com/generate_204"),
        integrated_panels=[dict(({k: v for k, v in p.items() if k != "pass"} if p.get("auto") else p), pkey=_pkey(p)) for p in _effective_panels()],
        panel_sync_seconds=get_global("panel_sync_seconds", "30"),
        main_host=_main_panel_host(),
        ext_subs=ext_subs
    ))

@app.post("/panel/sort-settings", response_class=HTMLResponse)
async def sort_settings_post(request: Request):
    if not authed(request): return RedirectResponse("/panel/login")
    form = await request.form()
    # Eski/cache'li sayfadan gelen kayit yeni ayarlari (json_apps vb.) SILMESIN:
    # guncel sayfa 'form_version' hidden alanini gonderir; yoksa kaydi yoksay.
    if not form.get("form_version"):
        return RedirectResponse("/panel/sort-settings", status_code=303)

    set_global("enable_sort", "1" if form.get("enable_sort") else "0")
    set_global("auto_rename_enabled", "1" if form.get("auto_rename_enabled") else "0")
    set_global("allowed_panels", form.get("allowed_panels","").strip())
    
    set_global("sort_order", form.get("sort_order","").strip() or ",".join(PROTOCOLS))
    set_global("vless_sort", form.get("vless_sort","").strip() or ",".join(VLESS_SUBTYPES))
    set_global("vmess_sort", form.get("vmess_sort","").strip() or ",".join(VMESS_SUBTYPES))
    set_global("extra_position", form.get("extra_position","top").strip() or "top")
    set_global("extra_content", form.get("extra_content",""))
    set_global("proxy_error_message", form.get("proxy_error_message","").strip() or "Sunucu gecici olarak kullanilamiyor.")
    set_global("source_404_message", form.get("source_404_message","").strip() or "Kaynak abonelik bulunamadi (404).")
    set_global("external_sub_interval", form.get("external_sub_interval","60").strip())
    set_global("ping_timeout", form.get("ping_timeout", "2.5").strip())
    try: set_global("ping_max_ms", str(max(0, int(float(form.get("ping_max_ms", "0") or 0)))))
    except Exception: pass
    set_global("cdn_label_enabled", "1" if form.get("cdn_label_enabled") else "0")
    set_global("vps_label_enabled", "1" if form.get("vps_label_enabled") else "0")
    set_global("vps_providers", ",".join(form.getlist("vps_provider")) or "azure,amazon,google,oracle")
    set_global("cdn_labels", json.dumps({
        "google": form.get("cdn_lbl_google", "GOOGLE"),
        "cloudfront": form.get("cdn_lbl_cloudfront", "CLOUDFRONT"),
        "fastly": form.get("cdn_lbl_fastly", "FASTLY")}))
    set_global("vps_labels", json.dumps({
        "azure": form.get("vps_lbl_azure", "AZURE"),
        "amazon": form.get("vps_lbl_amazon", "AWS"),
        "google": form.get("vps_lbl_google", "GOOGLE"),
        "oracle": form.get("vps_lbl_oracle", "ORACLE")}))
    cp = form.get("client_profiles_json")
    if cp is not None:
        try:
            parsed = json.loads(cp)
            if isinstance(parsed, list): set_global("client_profiles", json.dumps(parsed))
        except: pass
    # JSON abonelik ayarlari
    set_global("json_apps", form.get("json_apps", "").strip())
    set_global("json_direct_domains", form.get("json_direct_domains", ""))
    set_global("json_direct_ips", form.get("json_direct_ips", ""))
    set_global("json_block_ads", "1" if form.get("json_block_ads") else "0")
    set_global("json_balancer", "1" if form.get("json_balancer") else "0")
    set_global("json_balancer_ext", "1" if form.get("json_balancer_ext") else "0")
    set_global("json_balancer_name", form.get("json_balancer_name", "⚡ Auto (fastest)").strip() or "⚡ Auto (fastest)")
    set_global("json_balancer_strategy", form.get("json_balancer_strategy", "leastPing").strip() or "leastPing")
    set_global("json_balancer_pos", form.get("json_balancer_pos", "first").strip() or "first")
    set_global("json_balancer_interval", form.get("json_balancer_interval", "1m").strip() or "1m")
    set_global("json_balancer_url", form.get("json_balancer_url", "https://www.gstatic.com/generate_204").strip() or "https://www.gstatic.com/generate_204")

    if form.get("panel_sync_seconds") is not None:
        try:
            v = int(form.get("panel_sync_seconds") or 0)
            set_global("panel_sync_seconds", str(0 if v <= 0 else max(10, v)))
        except ValueError: pass
    pj = form.get("panels_json")
    if pj is not None:
        try:
            parsed = json.loads(pj)
            if isinstance(parsed, list): set_global("integrated_panels", json.dumps(parsed))
        except: pass

    ext_json = form.get("ext_json")
    if ext_json: set_global("external_subs_data", ext_json)

    global _last_external_fetch_time; _last_external_fetch_time = 0
    return RedirectResponse("/panel/sort-settings", status_code=303)

# Ek sub yollari (.env: EXTRA_SUB_PATHS=yol1,yol2). Varsayilan /macros her zaman acik; eski musteri
# linkleri baska bir yol kullaniyorsa (ör. /alexander77) buraya eklenir ki linkler bozulmasin.
for _sp in [x.strip().strip("/") for x in os.environ.get("EXTRA_SUB_PATHS", "").split(",") if x.strip().strip("/")]:
    if _sp in ("macros", "panel", "static", "healthcheck"): continue
    app.add_api_route(f"/{_sp}/{{path:path}}", sub_proxy_path, methods=["GET"])
    app.add_api_route(f"/{_sp}", sub_proxy, methods=["GET"])
