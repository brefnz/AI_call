"""
Seed test: 1 topic + 1 campaign + 1 target (nomor tes) untuk menguji pipeline 9Router.

Jalankan: python scripts/seed_test.py [nomor_tujuan]
Default nomor: 085704057231
"""
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import Database, new_id, now_iso  # noqa: E402


async def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else "085704057231"
    db = Database()

    # bersihkan target/campaign tes lama (kalau ada) supaya tidak double-dial
    await db.execute("DELETE FROM campaign_target WHERE phone_number=?", (phone,))

    topic_id = new_id("topic")
    await db.execute(
        """INSERT INTO topic (id, name, description, objective, system_instruction, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            topic_id,
            "Tes AI Voice 9Router",
            "Tes end-to-end pipeline STT -> LLM -> TTS",
            "Menghubungi nomor tes untuk memverifikasi pipeline AI voice berjalan: "
            "menyapa, konfirmasi audio terdengar jelas, lalu bertanya kabar.",
            "",  # kosong -> pakai BASE_RULES_ID default (outbound)
            "ACTIVE", now_iso(), now_iso(),
        ),
    )

    questions = [
        ("Sapa penelepon dengan ramah, perkenalkan diri sebagai asisten AI untuk tes koneksi, "
         "lalu tanyakan apakah penelepon dapat mendengar suara Anda dengan jelas.", 1),
        ("Tanyakan bagaimana kabar penelepon hari ini.", 1),
    ]
    for order_no, (text, mandatory) in enumerate(questions, start=1):
        await db.execute(
            """INSERT INTO topic_question
               (id, topic_id, question_text, question_order, is_mandatory, answer_type, status)
               VALUES (?,?,?,?,?,?,?)""",
            (f"{topic_id}_Q{order_no}", topic_id, text, order_no, mandatory, "text", "ACTIVE"),
        )

    campaign_id = new_id("camp")
    today = date.today()
    await db.execute(
        """INSERT INTO campaign
           (id, name, topic_id, start_date, end_date, call_start_time, call_end_time,
            concurrent_limit, max_retry, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            campaign_id, f"Tes AI Voice ({phone})", topic_id,
            today.isoformat(), (today + timedelta(days=7)).isoformat(),
            "00:00", "23:59", 1, 1, "ACTIVE", now_iso(), now_iso(),
        ),
    )

    target_id = new_id("tgt")
    await db.execute(
        """INSERT INTO campaign_target
           (id, campaign_id, phone_number, customer_data, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (target_id, campaign_id, phone, json.dumps({"nama": "Tes Pelanggan"}, ensure_ascii=False),
         "PENDING", now_iso(), now_iso()),
    )

    print(f"Seed selesai: topic={topic_id} campaign={campaign_id} target={target_id} phone={phone}")


if __name__ == "__main__":
    asyncio.run(main())
