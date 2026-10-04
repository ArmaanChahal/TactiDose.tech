"""Direct Python integration: import the service and drive one check-in.

    python examples/python_usage.py
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from tactidose_wellbeing import ActionRequest, StartSessionRequest, WellbeingService
from tactidose_wellbeing.config import load_safety_config
from tactidose_wellbeing.storage import InMemorySessionStore, SQLiteCheckinRepository

# Dependency injection: swap SQLiteCheckinRepository for any CheckinRepository
# implementation (e.g. a future TiDB repository) without changing the workflow.
db_path = Path(tempfile.mkdtemp()) / "example.sqlite3"
service = WellbeingService(
    repository=SQLiteCheckinRepository(db_path),
    sessions=InMemorySessionStore(),
    safety=load_safety_config(Path(__file__).parent.parent / "config" / "wellbeing.example.json"),
    session_ttl=timedelta(minutes=30),
)

USER = "synthetic-python-user-1"  # the host's authenticated user id

resp = service.start_session(USER, StartSessionRequest(request_id="py-1"))
print("TTS <", resp.speech_text)

for i, (action, text) in enumerate(
    [
        ("answer", "yes"),             # storage consent (covers ratings and notes)
        ("answer", "good"),
        ("add_note", "Finished a good book"),
        ("confirm", None),
        ("skip", None),                # stress skipped -> missing value
        ("answer", "okay"),
        ("reject", None),              # no note for sleep
        ("answer", "no"),              # no human support requested
        ("finish", None),
    ],
    start=2,
):
    kwargs = {"answer": text} if action == "answer" else {"note_text": text} if action == "add_note" else {}
    resp = service.handle_action(
        USER,
        resp.session_id,
        ActionRequest(request_id=f"py-{i}", action=action, expected_step=resp.step, **kwargs),
    )
    print(f"[{resp.session_status.value}] TTS <", resp.speech_text)
    if resp.pending_input:
        print("     unconfirmed:", resp.pending_input.model_dump(exclude_none=True))

history = service.get_history(USER)
print("\nSaved records:", len(history.records))
for answer in history.records[0].answers:
    print(f"  {answer.question_id.value:8} {answer.status.value:9} {answer.answer_value!s:6} note={answer.note_text!r}")

print("\nDeleting:", service.delete_history(USER).speech_text)
