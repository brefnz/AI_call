"""
Migrasi skema untuk mode inbound. Jalankan SEKALI, dengan aplikasi dimatikan:

    python scripts/migrate_inbound.py [path_db]

Yang dilakukan (aman dijalankan ulang):
  1. Backup file DB ke <db>.bak-inbound
  2. Tambah kolom di call_session: direction, caller_number, routed_queue, ai_summary
  3. Kalau call_session.campaign_target_id masih NOT NULL, tabel dibangun ulang
     tanpa NOT NULL itu (panggilan inbound tidak punya campaign target).
"""
import os
import re
import shutil
import sqlite3
import sys

NEW_COLUMNS = {
    "direction": "TEXT DEFAULT 'OUTBOUND'",
    "caller_number": "TEXT",
    "routed_queue": "TEXT",
    "ai_summary": "TEXT",
}


def _columns(con):
    return {r[1]: r for r in con.execute("PRAGMA table_info(call_session)")}


def _rebuild_without_notnull(con):
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='call_session'"
    ).fetchone()[0]
    new_ddl, n = re.subn(
        r"(\bcampaign_target_id\b[^,()]*?)\s+NOT\s+NULL", r"\1", ddl, count=1, flags=re.IGNORECASE
    )
    if n != 1:
        raise SystemExit(
            "Tidak bisa menghapus NOT NULL otomatis dari DDL berikut; ubah manual:\n" + ddl
        )
    new_ddl = re.sub(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?call_session[\"'`\]]?",
        "CREATE TABLE call_session_new",
        new_ddl, count=1, flags=re.IGNORECASE,
    )
    indexes = [
        r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='call_session' AND sql IS NOT NULL"
        )
    ]
    # Prosedur resmi SQLite: buat tabel baru, salin, drop lama, rename baru.
    # (Jangan RENAME tabel lama dulu; itu membuat FK tabel lain ikut menunjuk ke nama lama.)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN")
    con.execute(new_ddl)
    con.execute("INSERT INTO call_session_new SELECT * FROM call_session")
    con.execute("DROP TABLE call_session")
    con.execute("ALTER TABLE call_session_new RENAME TO call_session")
    for sql in indexes:
        con.execute(sql)
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        con.execute("ROLLBACK")
        raise SystemExit(f"foreign_key_check gagal, dibatalkan: {bad[:3]}")
    con.execute("COMMIT")
    con.execute("PRAGMA foreign_keys=ON")


def migrate(db_path: str):
    if not os.path.exists(db_path):
        raise SystemExit(f"DB tidak ditemukan: {db_path}")
    bak = db_path + ".bak-inbound"
    if not os.path.exists(bak):
        shutil.copy2(db_path, bak)
        print(f"Backup: {bak}")

    con = sqlite3.connect(db_path, isolation_level=None)   # transaksi diatur manual
    try:
        cols = _columns(con)
        if not cols:
            raise SystemExit("Tabel call_session tidak ada")
        for name, ddl in NEW_COLUMNS.items():
            if name not in cols:
                con.execute(f"ALTER TABLE call_session ADD COLUMN {name} {ddl}")
                print(f"+ kolom {name}")
        cols = _columns(con)
        if "campaign_target_id" in cols and cols["campaign_target_id"][3]:
            print("campaign_target_id masih NOT NULL -> bangun ulang tabel call_session")
            _rebuild_without_notnull(con)
        else:
            print("campaign_target_id sudah boleh NULL")
        print("Selesai.")
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from config import settings
        path = settings.db_path
    migrate(path)
