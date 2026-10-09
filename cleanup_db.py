import sqlite3

import os
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

print("==========================================")
print("🧹 Veritabanı Klon Temizliği Başlıyor...")
print("==========================================")

# Aynı isme sahip (kopya) subları bul
duplicates = con.execute("SELECT name, COUNT(*) as c FROM subs GROUP BY name HAVING c > 1").fetchall()

if not duplicates:
    print("✅ Hiçbir kopya bulunamadı! Veritabanı zaten tertemiz.")
else:
    for dup in duplicates:
        name = dup["name"]
        print(f"\n🔍 [{name}] için {dup['c']} adet kopya kayıt bulundu.")
        
        # Bu isme ait tüm ID'leri al (En yüksek ID en yenisidir)
        rows = con.execute("SELECT id FROM subs WHERE name=? ORDER BY id DESC", (name,)).fetchall()
        
        keep_id = rows[0]["id"]
        delete_ids = [r["id"] for r in rows[1:]]
        
        print(f"   -> Orijinal kayıt (ID: {keep_id}) korunuyor...")
        
        for did in delete_ids:
            # Eski kayda ait cihazları, logları ve trafiği sil
            con.execute("DELETE FROM devices WHERE sub_id=?", (did,))
            con.execute("DELETE FROM logs WHERE sub_id=?", (did,))
            con.execute("DELETE FROM traffic WHERE sub_id=?", (did,))
            # Eski sub kaydını sil
            con.execute("DELETE FROM subs WHERE id=?", (did,))
            print(f"   🗑️ Eski kopya silindi (ID: {did})")

    con.commit()
    print("\n==========================================")
    print("✨ Temizlik tamamlandı! Bütün çöpler yok edildi.")
    print("==========================================")

con.close()
