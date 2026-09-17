"""
Seed contoh data sesuai PRD #7 (Konfigurasi Topik) dan #33 (Contoh End-to-End).
Jalankan sekali: python scripts/seed_example.py

Versi ini bikin 2 topic + 2 campaign + 2 target (extension 101 & 102) buat
test call ke 2 extension MicroSIP yang beda topic.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import Database, new_id, now_iso  # noqa: E402


async def seed_one(db: Database, topic_name: str, topic_desc: str, topic_objective: str,
                    questions: list, campaign_name: str, phone_number: str):
    topic_id = new_id("topic")
    await db.execute(
        """INSERT INTO topic (id, name, description, objective, system_instruction, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            topic_id, topic_name, topic_desc, topic_objective,
            "", "ACTIVE", now_iso(), now_iso(),
        ),
    )

    for order_no, (text, mandatory, atype) in enumerate(questions, start=1):
        await db.execute(
            """INSERT INTO topic_question
               (id, topic_id, question_text, question_order, is_mandatory, answer_type, status)
               VALUES (?,?,?,?,?,?,?)""",
            (f"{topic_id}_Q{order_no}", topic_id, text, order_no, int(mandatory), atype, "ACTIVE"),
        )

    campaign_id = new_id("camp")
    today = date.today()
    await db.execute(
        """INSERT INTO campaign
           (id, name, topic_id, start_date, end_date, call_start_time, call_end_time,
            concurrent_limit, max_retry, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            campaign_id, campaign_name, topic_id,
            today.isoformat(), (today + timedelta(days=7)).isoformat(),
            "08:00", "20:00", 5, 2, "ACTIVE", now_iso(), now_iso(),
        ),
    )

    # Nomor tujuan test — diisi nomor extension MicroSIP (bukan nomor HP asli),
    # karena ARI_OUTBOUND_ENDPOINT_TEMPLATE diarahkan langsung ke extension.
    target_id = new_id("tgt")
    await db.execute(
        """INSERT INTO campaign_target
           (id, campaign_id, phone_number, customer_data, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (target_id, campaign_id, phone_number, "{}", "PENDING", now_iso(), now_iso()),
    )

    print(f"Seed selesai: topic={topic_id} campaign={campaign_id} target={target_id} phone={phone_number}")


async def main():
    db = Database()

    await seed_one(
        db,
        topic_name="Kepuasan Pelanggan",
        topic_desc="Survey kepuasan pelanggan pasca layanan",
        topic_objective="Mengetahui tingkat kepuasan pelanggan terhadap layanan.",
        questions=[
            ("Bagaimana tingkat kepuasan Anda terhadap pelayanan kami?", True, "text"),
            ("Apakah masalah yang Anda alami sebelumnya sudah terselesaikan?", True, "yes_no"),
            ("Apakah Anda memiliki saran untuk meningkatkan pelayanan kami?", False, "text"),
        ],
        campaign_name="Survey Kepuasan Pelanggan - Ext 101",
        phone_number="101",
    )

    await seed_one(
        db,
        topic_name="Konfirmasi Pembayaran",
        topic_desc="Konfirmasi status pembayaran pelanggan",
        topic_objective="Memastikan pelanggan sudah menerima info tagihan dan mengetahui status pembayarannya.",
        questions=[
            ("Apakah Anda sudah menerima tagihan bulan ini?", True, "yes_no"),
            ("Apakah pembayaran sudah dilakukan?", True, "yes_no"),
        ],
        campaign_name="Konfirmasi Pembayaran - Ext 102",
        phone_number="102",
    )


if __name__ == "__main__":
    asyncio.run(main())
