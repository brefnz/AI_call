"""
Reporting (PRD #23 report per-call, #24 dashboard campaign).
"""
from db.database import Database


async def call_report(db: Database, call_session_id: str) -> dict:
    session = await db.fetchone("SELECT * FROM call_session WHERE id=?", (call_session_id,))
    if not session:
        return {}
    target = await db.fetchone(
        "SELECT * FROM campaign_target WHERE id=?", (session["campaign_target_id"],)
    )
    answers = await db.fetchall(
        "SELECT * FROM conversation_answer WHERE call_session_id=?", (call_session_id,)
    )
    transcript = await db.fetchall(
        "SELECT speaker, text, timestamp FROM transcript WHERE call_session_id=? ORDER BY timestamp",
        (call_session_id,),
    )
    return {
        "campaign_id": target["campaign_id"] if target else None,
        "phone_number": target["phone_number"] if target else None,
        "call_time": session["started_at"],
        "status": session["status"],
        "duration": session["duration"],
        "question_count": len(answers),
        "answered_count": sum(1 for a in answers if a["answer_status"] == "VALID"),
        "mandatory_completed": session["status"] == "COMPLETED",
        "conversation_result": session["conversation_result"],
        "call_outcome": session["outcome"],
        "transcript": transcript,
        "answers": answers,
    }


async def campaign_dashboard(db: Database, campaign_id: str) -> dict:
    rows = await db.fetchall(
        "SELECT status, COUNT(*) as cnt FROM campaign_target WHERE campaign_id=? GROUP BY status",
        (campaign_id,),
    )
    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return {
        "campaign_id": campaign_id,
        "total_target": total,
        "pending": counts.get("PENDING", 0),
        "calling": counts.get("CALLING", 0),
        "completed": counts.get("COMPLETED", 0),
        "no_answer": counts.get("NO_ANSWER", 0) + counts.get("FINAL_NO_ANSWER", 0),
        "busy": counts.get("BUSY", 0) + counts.get("FINAL_BUSY", 0),
        "failed": counts.get("FAILED", 0),
        "incomplete": counts.get("INCOMPLETE", 0),
        "raw_status_counts": counts,
    }
