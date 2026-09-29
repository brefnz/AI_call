"""
Kosongkan SEMUA data di database (campaign, topic, target, call_session,
transcript, conversation_answer, call_event_log) -- skema tabel TIDAK
dihapus, cuma isinya. Dipakai buat mulai bersih sebelum bikin blasting baru.

Jalankan dari root project (sebelah config.py):
    python reset_db.py

Akan minta konfirmasi dulu sebelum benar-benar menghapus apa pun.
"""
import asyncio

from config import settings
from db.database import Database

TABLES_IN_ORDER = [
    # urutan penting -- hapus child dulu sebelum parent (foreign key)
    "call_event_log",
    "transcript",
    "conversation_answer",
    "call_session",
    "campaign_target",
    "topic_question",
    "campaign",
    "topic",
]


async def main():
    db = Database(settings.db_path)

    counts = {}
    for table in TABLES_IN_ORDER:
        row = await db.fetchone(f"SELECT COUNT(*) as n FROM {table}")
        counts[table] = row["n"] if row else 0

    total = sum(counts.values())
    if total == 0:
        print("Database sudah kosong, tidak ada yang perlu dihapus.")
        return

    print(f"Database: {settings.db_path}")
    print("Data yang akan DIHAPUS PERMANEN:")
    for table, n in counts.items():
        if n > 0:
            print(f"  - {table}: {n} baris")

    confirm = input("\nKetik 'HAPUS' (huruf besar) untuk lanjut, apa pun selain itu akan membatalkan: ")
    if confirm.strip() != "HAPUS":
        print("Dibatalkan, tidak ada yang dihapus.")
        return

    for table in TABLES_IN_ORDER:
        await db.execute(f"DELETE FROM {table}")

    print(f"\nSelesai. {total} baris dihapus dari {len([t for t in counts if counts[t] > 0])} tabel.")
    print("Database sekarang kosong -- siap dipakai buat blasting baru dari dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
