"""
Seed: 1 topik (Kepuasan Pelanggan) dengan verifikasi identitas + pemberitahuan rekaman.
Semua question_text berupa ARAHAN untuk Gemini, bukan kalimat yang dibacakan persis.
Gemini yang menyusun kata-katanya sendiri (Bahasa Indonesia formal).

Jalankan sekali: python scripts/seed_example.py
"""
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import Database, new_id, now_iso  # noqa: E402

PERUSAHAAN = "Datakelola"
TUJUAN_REKAM = "peningkatan kualitas layanan, pelatihan, evaluasi, dan dokumentasi"


def eja_digit(nomor: str) -> str:
    """'0012345678' -> '0, 0, 1, 2, ...' supaya dibacakan angka per angka dengan jeda."""
    return ", ".join(nomor)


def build_questions(c: dict) -> list:
    """(arahan, mandatory, answer_type) — urutan = urutan alur percakapan.
    Teks di sini adalah INSTRUKSI untuk Gemini, bukan naskah yang dibacakan."""
    nama = c["nama"]
    nomor = eja_digit(c["nomor_pelanggan"])

    return [
        # --- Pembukaan + pemberitahuan rekaman + konfirmasi nama ---
        (f"Buka percakapan dengan salam. Perkenalkan diri sebagai asisten virtual dari {PERUSAHAAN}. "
         f"Beritahukan bahwa percakapan ini direkam untuk keperluan {TUJUAN_REKAM}. "
         f"Lalu konfirmasi apakah lawan bicara adalah {nama}.",
         True, "yes_no"),

        # --- Verifikasi nomor pelanggan ---
        (f"Setelah nama terkonfirmasi, mintalah verifikasi dengan menanyakan apakah nomor pelanggannya "
         f"{nomor}. Bacakan angka demi angka dengan jeda.",
         True, "yes_no"),

        # --- Persetujuan melanjutkan ---
        ("Tanyakan apakah pelanggan bersedia melanjutkan percakapan dengan sepengetahuan bahwa "
         "percakapan ini direkam.",
         True, "yes_no"),

        # --- Topik kepuasan pelanggan ---
        ("Beri transisi singkat, lalu tanyakan bagaimana penilaian pelanggan secara umum "
         "terhadap layanan yang telah diberikan.",
         True, "text"),
        ("Tanyakan apakah permasalahan yang sebelumnya dialami pelanggan sudah terselesaikan.",
         True, "yes_no"),
        ("Tanyakan apakah ada saran atau masukan untuk meningkatkan kualitas layanan. "
         "Boleh dilewati jika pelanggan tidak ingin menjawab.",
         False, "text"),
    ]


def build_objective(c: dict) -> str:
    return (
        f"Menghubungi pelanggan {c['nama']} (nomor pelanggan {c['nomor_pelanggan']}) atas nama "
        f"{PERUSAHAAN} untuk memverifikasi identitas dan mengetahui tingkat kepuasan terhadap layanan."
    )


def build_system_instruction(c: dict) -> str:
    return (
        f"Anda adalah asisten virtual dari {PERUSAHAAN} yang melakukan panggilan telepon.\n"
        "GAYA BAHASA:\n"
        "- Gunakan Bahasa Indonesia formal, sopan, dan ringkas. Sapa dengan \"Bapak atau Ibu\".\n"
        "- Setiap butir di daftar pertanyaan adalah ARAHAN untuk Anda, BUKAN naskah. "
        "Susun sendiri kalimat lisannya secara natural; jangan membacakan arahan itu secara harfiah "
        "dan jangan mengulang kalimat yang sama persis di tiap panggilan.\n"
        "- Satu giliran bicara satu pokok saja; tunggu jawaban pelanggan sebelum lanjut.\n"
        f"Pelanggan yang dihubungi: {c['nama']}, nomor pelanggan {c['nomor_pelanggan']}.\n"

        "GAYA SUARA:\n"
        "- Bicara dengan tempo sedang dan tenang, seperti petugas layanan pelanggan profesional. "
        "Jangan terlalu ceria atau terlalu datar.\n"
        "- Kalimat pernyataan diakhiri dengan intonasi turun. Kalimat tanya diakhiri dengan naik ringan.\n"
        "- Beri jeda singkat di antara kalimat, dan jeda sedikit lebih panjang sebelum pertanyaan inti.\n"
        "- Gunakan kalimat pendek, maksimal dua klausa.\n"
        "- Jangan menyisipkan kata pengisi seperti \"hmm\" atau \"eh\", dan jangan tertawa.\n"
        "PELAFALAN:\n"
        "- Ucapkan nama orang dengan pelan dan jelas, lalu beri jeda sebelum lanjut.\n"
        "- Bacakan nomor angka demi angka dengan jeda di setiap 3 atau 4 angka.\n"
        f"- Ucapkan nama perusahaan {PERUSAHAAN} dengan tenang, tiap suku kata jelas.\n"
        "- Hindari singkatan dan istilah Inggris; gunakan padanan Bahasa Indonesia yang lazim diucapkan.\n"

        "ATURAN:\n"
        f"1. Di awal percakapan, perkenalkan diri sebagai asisten virtual dari {PERUSAHAAN} dan "
        f"sampaikan bahwa percakapan direkam untuk keperluan {TUJUAN_REKAM}.\n"
        "2. Jangan menyebutkan data pelanggan apa pun sebelum pelanggan mengonfirmasi nama dan "
        "nomor pelanggannya.\n"
        "3. Jika yang menjawab bukan orang yang dituju, atau nomor pelanggan dibantah, mohon maaf, "
        "jangan menyebutkan data apa pun, lalu panggil end_conversation.\n"
        "4. Jika pelanggan keberatan percakapan direkam atau tidak bersedia melanjutkan, hormati "
        "keputusannya, ucapkan terima kasih, lalu panggil end_conversation.\n"
        "5. Bacakan nomor pelanggan angka demi angka dengan jeda.\n"
        "6. Catat setiap jawaban melalui record_answer. Jika pelanggan meminta berbicara dengan "
        "petugas, panggil request_human_agent.\n"
        "7. Setelah seluruh pertanyaan selesai, ucapkan terima kasih dan panggil end_conversation."
    )


async def seed_customer(db: Database, c: dict, phone_number: str):
    topic_id = new_id("topic")
    await db.execute(
        """INSERT INTO topic (id, name, description, objective, system_instruction, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            topic_id,
            f"Kepuasan Pelanggan - {c['nama']}",
            "Survei kepuasan pelanggan dengan verifikasi identitas dan pemberitahuan rekaman",
            build_objective(c),
            build_system_instruction(c),
            "ACTIVE", now_iso(), now_iso(),
        ),
    )

    for order_no, (text, mandatory, atype) in enumerate(build_questions(c), start=1):
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
            campaign_id, f"Survei Kepuasan - {c['nama']} (Ext {phone_number})", topic_id,
            today.isoformat(), (today + timedelta(days=7)).isoformat(),
            "08:00", "20:00", 5, 2, "ACTIVE", now_iso(), now_iso(),
        ),
    )

    customer_data = {"nama": c["nama"], "nomor_pelanggan": c["nomor_pelanggan"]}
    target_id = new_id("tgt")
    await db.execute(
        """INSERT INTO campaign_target
           (id, campaign_id, phone_number, customer_data, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (target_id, campaign_id, phone_number,
         json.dumps(customer_data, ensure_ascii=False), "PENDING", now_iso(), now_iso()),
    )

    print(f"Seed selesai: {c['nama']} | topic={topic_id} campaign={campaign_id} "
          f"target={target_id} phone={phone_number}")


async def main():
    db = Database()

    pelanggan = [
        ({"nama": "Budi Santoso", "nomor_pelanggan": "0012345678"}, "101"),
        ({"nama": "Siti Rahmawati", "nomor_pelanggan": "0087654321"}, "102"),
    ]

    for data, ext in pelanggan:
        await seed_customer(db, data, ext)


if __name__ == "__main__":
    asyncio.run(main())