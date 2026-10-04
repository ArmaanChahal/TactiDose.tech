"""TactiDose well-being check-in module (independent, non-clinical prototype).

Quick start::

    from tactidose_wellbeing import build_service, StartSessionRequest, ActionRequest

    service = build_service()                       # SQLite at data/wellbeing.sqlite3
    r = service.start_session("user-1", StartSessionRequest(request_id="r1"))
    r = service.handle_action("user-1", r.session_id,
                              ActionRequest(request_id="r2", action="answer", answer="yes"))
    print(r.speech_text)
"""

from .bootstrap import build_app, build_service
from .contract import (
    SCHEMA_VERSION,
    ActionRequest,
    CheckinResponse,
    DeleteResponse,
    HistoryResponse,
    StartSessionRequest,
)
from .service import ServiceError, WellbeingService

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION",
    "ActionRequest",
    "CheckinResponse",
    "DeleteResponse",
    "HistoryResponse",
    "ServiceError",
    "StartSessionRequest",
    "WellbeingService",
    "build_app",
    "build_service",
]
