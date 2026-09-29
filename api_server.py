"""
Backend HTTP API untuk dashboard (Blasting / Wallboard / Riwayat Call).

Belum ada di repo asli -- README AI_call sendiri bilang ini item #7 yang masih
perlu dikerjakan ("Belum ada dashboard web/API HTTP ... tinggal dibungkus
FastAPI/Flask kalau perlu endpoint HTTP"). File ini murni membungkus
db/database.py dan core/reporting.py yang sudah ada -- TIDAK mengubah skema,
TIDAK mengubah campaign_manager/call_session, supaya proses calling yang
sudah jalan tidak tersentuh sama sekali. Dashboard ini baca (dan untuk
campaign/topic/target, tulis) ke database SQLite yang sama yang dipakai
main.py -- keduanya boleh jalan bersamaan (SQLite dengan WAL mode aman untuk
1 writer + banyak reader ringan seperti ini).

Cara jalan (dari root folder project, sebelah config.py):
    pip install fastapi uvicorn
    uvicorn api_server:app --reload --port 8090

Lalu buka dashboard.html di browser (bisa langsung dobel-klik file-nya, atau
`python -m http.server 8091` di folder yang sama lalu buka
http://localhost:8091/dashboard.html) -- dashboard akan manggil API ini di
http://localhost:8090.

CATATAN KEAMANAN: CORS di sini dibuka untuk semua origin (allow_origins=["*"])
supaya dashboard.html gampang dites lokal. Untuk pemakaian di jaringan
kantor/produksi, ganti ke daftar origin spesifik dan tambahkan auth (mis.
API key sederhana di header) sebelum expose ke jaringan yang lebih luas dari
laptop kamu sendiri.
"""
import json
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from core.reporting import call_report, campaign_dashboard
from db.database import Database, new_id, now_iso

app = FastAPI(title="AI Outbound Calling — Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database(settings.db_path)


# ---------------------------------------------------------------- schemas --

class QuestionIn(BaseModel):
    text: str
    order: int
    is_mandatory: bool = True
    answer_type: str = "text"
    validation_rule: Optional[str] = None
    next_question: Optional[str] = None


class TopicIn(BaseModel):
    name: str
    description: Optional[str] = None
    objective: str
    system_instruction: Optional[str] = ""
    questions: list[QuestionIn] = []


class CampaignIn(BaseModel):
    name: str
    topic_id: str
    start_date: str          # 'YYYY-MM-DD'
    end_date: str            # 'YYYY-MM-DD'
    call_start_time: str     # 'HH:MM'
    call_end_time: str       # 'HH:MM'
    concurrent_limit: int = 5
    max_retry: int = 2
    status: str = "ACTIVE"   # ACTIVE, PAUSED, COMPLETED


class TargetIn(BaseModel):
    phone_number: str
    customer_data: Optional[dict] = None


class TargetsBulkIn(BaseModel):
    targets: list[TargetIn]


# ------------------------------------------------------------------ topic --

@app.get("/api/topics")
async def list_topics():
    return await db.fetchall("SELECT * FROM topic ORDER BY created_at DESC")


@app.get("/api/topics/{topic_id}")
async def get_topic(topic_id: str):
    topic = await db.fetchone("SELECT * FROM topic WHERE id=?", (topic_id,))
    if not topic:
        raise HTTPException(404, "Topic tidak ditemukan")
    topic["questions"] = await db.fetchall(
        "SELECT * FROM topic_question WHERE topic_id=? ORDER BY question_order",
        (topic_id,),
    )
    return topic


@app.post("/api/topics")
async def create_topic(payload: TopicIn):
    topic_id = new_id("topic")
    await db.execute(
        """INSERT INTO topic (id, name, description, objective, system_instruction, status)
           VALUES (?,?,?,?,?, 'ACTIVE')""",
        (topic_id, payload.name, payload.description, payload.objective,
         payload.system_instruction or ""),
    )
    for q in payload.questions:
        await db.execute(
            """INSERT INTO topic_question
               (id, topic_id, question_text, question_order, is_mandatory,
                answer_type, validation_rule, next_question, status)
               VALUES (?,?,?,?,?,?,?,?, 'ACTIVE')""",
            (new_id("q"), topic_id, q.text, q.order, int(q.is_mandatory),
             q.answer_type, q.validation_rule, q.next_question),
        )
    return {"id": topic_id}


# --------------------------------------------------------------- campaign --

@app.get("/api/campaigns")
async def list_campaigns():
    rows = await db.fetchall(
        """SELECT c.*, t.name AS topic_name
           FROM campaign c LEFT JOIN topic t ON t.id = c.topic_id
           ORDER BY c.created_at DESC"""
    )
    for c in rows:
        counts = await db.fetchall(
            "SELECT status, COUNT(*) cnt FROM campaign_target WHERE campaign_id=? GROUP BY status",
            (c["id"],),
        )
        c["target_count"] = sum(x["cnt"] for x in counts)
    return rows


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    c = await db.fetchone(
        """SELECT c.*, t.name AS topic_name
           FROM campaign c LEFT JOIN topic t ON t.id = c.topic_id
           WHERE c.id=?""",
        (campaign_id,),
    )
    if not c:
        raise HTTPException(404, "Campaign tidak ditemukan")
    return c


@app.post("/api/campaigns")
async def create_campaign(payload: CampaignIn):
    topic = await db.fetchone("SELECT id FROM topic WHERE id=?", (payload.topic_id,))
    if not topic:
        raise HTTPException(400, "topic_id tidak valid — buat/pilih topic dulu")
    campaign_id = new_id("camp")
    await db.execute(
        """INSERT INTO campaign
           (id, name, topic_id, start_date, end_date, call_start_time, call_end_time,
            concurrent_limit, max_retry, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (campaign_id, payload.name, payload.topic_id, payload.start_date, payload.end_date,
         payload.call_start_time, payload.call_end_time, payload.concurrent_limit,
         payload.max_retry, payload.status),
    )
    return {"id": campaign_id}


@app.put("/api/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, payload: CampaignIn):
    existing = await db.fetchone("SELECT id FROM campaign WHERE id=?", (campaign_id,))
    if not existing:
        raise HTTPException(404, "Campaign tidak ditemukan")
    await db.execute(
        """UPDATE campaign SET name=?, topic_id=?, start_date=?, end_date=?,
           call_start_time=?, call_end_time=?, concurrent_limit=?, max_retry=?,
           status=?, updated_at=? WHERE id=?""",
        (payload.name, payload.topic_id, payload.start_date, payload.end_date,
         payload.call_start_time, payload.call_end_time, payload.concurrent_limit,
         payload.max_retry, payload.status, now_iso(), campaign_id),
    )
    return {"ok": True}


@app.delete("/api/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    await db.execute("DELETE FROM campaign_target WHERE campaign_id=?", (campaign_id,))
    await db.execute("DELETE FROM campaign WHERE id=?", (campaign_id,))
    return {"ok": True}


# ---------------------------------------------------------- campaign target --

@app.get("/api/campaigns/{campaign_id}/targets")
async def list_targets(campaign_id: str):
    return await db.fetchall(
        "SELECT * FROM campaign_target WHERE campaign_id=? ORDER BY created_at DESC",
        (campaign_id,),
    )


@app.post("/api/campaigns/{campaign_id}/targets")
async def add_targets(campaign_id: str, payload: TargetsBulkIn):
    campaign = await db.fetchone("SELECT id FROM campaign WHERE id=?", (campaign_id,))
    if not campaign:
        raise HTTPException(404, "Campaign tidak ditemukan")
    inserted = 0
    for t in payload.targets:
        phone = t.phone_number.strip()
        if not phone:
            continue
        await db.execute(
            """INSERT INTO campaign_target
               (id, campaign_id, phone_number, customer_data, status)
               VALUES (?,?,?,?, 'PENDING')""",
            (new_id("tgt"), campaign_id, phone,
             json.dumps(t.customer_data) if t.customer_data else None),
        )
        inserted += 1
    return {"inserted": inserted}


@app.delete("/api/targets/{target_id}")
async def delete_target(target_id: str):
    await db.execute("DELETE FROM campaign_target WHERE id=?", (target_id,))
    return {"ok": True}


# -------------------------------------------------------------- wallboard --

WALLBOARD_STATUS_GROUPS = {
    "connected": ("CONNECTED", "COMPLETED", "INCOMPLETE"),
    "no_answer": ("NO_ANSWER", "FINAL_NO_ANSWER"),
    "busy": ("BUSY", "FINAL_BUSY"),
    "failed": ("FAILED", "FINAL_FAILED"),
    "rejected": ("REJECTED", "FINAL_REJECTED"),
    "pending": ("PENDING",),
    "in_progress": ("CALLING",),
}


@app.get("/api/wallboard")
async def wallboard(campaign_id: Optional[str] = None):
    where = "WHERE campaign_id=?" if campaign_id else ""
    params = (campaign_id,) if campaign_id else ()
    rows = await db.fetchall(
        f"SELECT status, COUNT(*) cnt FROM campaign_target {where} GROUP BY status", params
    )
    counts = {r["status"]: r["cnt"] for r in rows}

    def group_sum(key):
        return sum(counts.get(s, 0) for s in WALLBOARD_STATUS_GROUPS[key])

    total = sum(counts.values())
    connected = group_sum("connected")

    dur_where = "WHERE cs.duration IS NOT NULL"
    dur_params = ()
    if campaign_id:
        dur_where += " AND ct.campaign_id=?"
        dur_params = (campaign_id,)
    dur_row = await db.fetchone(
        f"""SELECT AVG(cs.duration) avg_dur, SUM(cs.duration) total_dur, COUNT(*) n
            FROM call_session cs JOIN campaign_target ct ON ct.id = cs.campaign_target_id
            {dur_where}""",
        dur_params,
    )

    active_where = "WHERE cs.status IN ('CALLING','CONNECTED','IN_PROGRESS')"
    active_params = ()
    if campaign_id:
        active_where += " AND ct.campaign_id=?"
        active_params = (campaign_id,)
    active_row = await db.fetchone(
        f"""SELECT COUNT(*) n FROM call_session cs
            JOIN campaign_target ct ON ct.id = cs.campaign_target_id {active_where}""",
        active_params,
    )

    return {
        "total_target": total,
        "connected": connected,
        "no_answer": group_sum("no_answer"),
        "busy": group_sum("busy"),
        "failed": group_sum("failed") + group_sum("rejected"),
        "pending": group_sum("pending"),
        "calls_in_progress": (active_row or {}).get("n", 0),
        "answer_rate_pct": round(connected / total * 100, 2) if total else 0.0,
        "avg_call_duration_sec": round(dur_row["avg_dur"], 1) if dur_row and dur_row["avg_dur"] else 0,
        "total_call_duration_sec": dur_row["total_dur"] if dur_row and dur_row["total_dur"] else 0,
        "raw_status_counts": counts,
        "generated_at": now_iso(),
    }


# ------------------------------------------------------------------ calls --

@app.get("/api/calls")
async def list_calls(
    campaign_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    where = ["1=1"]
    params: list = []
    if campaign_id:
        where.append("ct.campaign_id=?")
        params.append(campaign_id)
    if status:
        where.append("cs.status=?")
        params.append(status)
    where_sql = " AND ".join(where)

    rows = await db.fetchall(
        f"""SELECT cs.id, cs.status, cs.outcome, cs.conversation_result,
                   cs.started_at, cs.connected_at, cs.ended_at, cs.duration,
                   ct.phone_number, ct.customer_data, ct.campaign_id,
                   c.name AS campaign_name
            FROM call_session cs
            JOIN campaign_target ct ON ct.id = cs.campaign_target_id
            JOIN campaign c ON c.id = ct.campaign_id
            WHERE {where_sql}
            ORDER BY cs.started_at DESC
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    return rows


@app.get("/api/calls/{call_session_id}/transcript")
async def get_call_transcript(call_session_id: str):
    report = await call_report(db, call_session_id)
    if not report:
        raise HTTPException(404, "Call session tidak ditemukan")
    return report


# ------------------------------------------------------------------- misc --

@app.get("/api/health")
async def health():
    return {"ok": True, "time": now_iso()}
