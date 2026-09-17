"""
State machine (PRD #10).

Catatan desain penting: diagram PRD (INIT -> CALLING -> ... -> LISTENING ->
PROCESSING -> RESPONDING -> VALIDATING -> ...) menggambarkan siklus per-turn
ASR/TTS klasik. Karena Gemini Live menangani STT+TTS+turn-taking secara
internal dalam satu stream audio dua arah, aplikasi tidak punya visibilitas
granular ke tiap sub-state itu (kapan persis "LISTENING" vs "PROCESSING").

Jadi state machine ini dipecah dua level, sesuai apa yang benar-benar bisa
dikontrol/diamati aplikasi:
  - CallState: level panggilan (telephony + sesi Gemini), ini yang 1:1 dengan
    diagram besar (INIT/CALLING/CONNECTED/IN_PROGRESS/COMPLETED/HANGUP/...).
  - Question-level tracking (mandatory/answered/pending) yang menggantikan
    ASKING/LISTENING/PROCESSING/RESPONDING/VALIDATING/CLARIFICATION/REDIRECT —
    transisi ini didorong oleh tool call `record_answer` dari Gemini (lapisan
    Application Validation, PRD #17), bukan oleh asumsi state audio internal.
"""
from dataclasses import dataclass, field
from enum import Enum

from core.topic_config import Question, Topic


class CallState(str, Enum):
    INIT = "INIT"
    CALLING = "CALLING"
    CONNECTED = "CONNECTED"
    IN_PROGRESS = "IN_PROGRESS"     # sesi Gemini aktif, audio mengalir dua arah
    COMPLETED = "COMPLETED"         # semua mandatory terjawab, ditutup normal
    INCOMPLETE = "INCOMPLETE"       # ditutup sebelum mandatory selesai
    FAILED = "FAILED"
    HANGUP = "HANGUP"


class AnswerStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNCLEAR = "UNCLEAR"
    SKIPPED = "SKIPPED"
    REFUSED = "REFUSED"


@dataclass
class QuestionProgress:
    question: Question
    status: str = "PENDING"     # PENDING, VALID, INVALID, UNCLEAR, SKIPPED, REFUSED
    answer_text: str = None
    confidence: float = None


class ConversationState:
    """Dipegang satu instance per call session."""

    def __init__(self, topic: Topic):
        self.topic = topic
        self.call_state = CallState.INIT
        self.progress: dict[str, QuestionProgress] = {
            q.id: QuestionProgress(question=q) for q in topic.all_ordered()
        }
        self.human_agent_requested = False
        self.end_requested = False
        self.end_summary: str = ""

    # ---- query helpers ----

    def current_question(self) -> Question | None:
        for q in self.topic.all_ordered():
            if self.progress[q.id].status == "PENDING":
                return q
        return None

    def mandatory_pending(self) -> list[Question]:
        return [
            q for q in self.topic.mandatory_questions()
            if self.progress[q.id].status == "PENDING"
        ]

    def mandatory_complete(self) -> bool:
        return len(self.mandatory_pending()) == 0

    def answered_summary(self) -> dict:
        return {
            qid: {"status": p.status, "answer": p.answer_text}
            for qid, p in self.progress.items()
        }

    # ---- mutation (dipanggil dari tool handler saat Gemini memanggil record_answer) ----

    def record_answer(self, question_id: str, answer_text: str, status: str, confidence: float = None):
        if question_id not in self.progress:
            return False
        p = self.progress[question_id]
        p.answer_text = answer_text
        p.status = status
        p.confidence = confidence
        return True

    def request_human_agent(self):
        self.human_agent_requested = True

    def request_end(self, summary: str):
        self.end_requested = True
        self.end_summary = summary

    # ---- hasil akhir (PRD #21, #22) ----

    def compute_outcome(self, telephony_connected: bool) -> str:
        if not telephony_connected:
            return "FAILED"
        if self.human_agent_requested:
            return "REQUEST_HUMAN_AGENT"
        refused_mandatory = [
            q for q in self.topic.mandatory_questions()
            if self.progress[q.id].status == "REFUSED"
        ]
        if refused_mandatory and not self.mandatory_pending():
            return "CALLER_REFUSED"
        if self.mandatory_complete():
            return "SUCCESS"
        answered_any = any(p.status != "PENDING" for p in self.progress.values())
        return "PARTIAL_SUCCESS" if answered_any else "INCOMPLETE"

    def compute_quality(self) -> str:
        total = len(self.topic.mandatory_questions())
        if total == 0:
            return "GOOD"
        valid = sum(
            1 for q in self.topic.mandatory_questions()
            if self.progress[q.id].status == "VALID"
        )
        ratio = valid / total
        if ratio >= 0.99:
            return "GOOD"
        if ratio >= 0.5:
            return "PARTIAL"
        return "POOR"
