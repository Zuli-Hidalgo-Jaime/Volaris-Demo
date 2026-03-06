# router.py
import re
from dataclasses import dataclass, field

AFFIRMATIONS = {"s\u00ed", "si", "ok", "va", "dale", "de acuerdo", "confirmo", "aja", "aj\u00e1"}

def _norm(t: str) -> str:
    s = (t or "").strip().lower()
    # Acepta afirmaciones aunque incluyan signos al inicio/fin.
    s = s.strip("¿?¡!.,;:()[]{}\"'` ")
    return re.sub(r"\s+", " ", s)

def is_affirmation_or_short(user_text: str) -> bool:
    t = _norm(user_text)
    
    if t in AFFIRMATIONS:
        return True
    
    if re.fullmatch(r"(y\s+)?por\s+\d+(\.\d+)?", t):
        return True

    return bool(re.fullmatch(r"\d+(\.\d+)?", t))

@dataclass
class Session:
    # Hilo/logical thread por agente (en este stack se respalda con previous_response_id)
    router_thread_id: str | None = None
    politicas_thread_id: str | None = None
    gastos_thread_id: str | None = None
    threads_by_domain: dict[str, str] = field(default_factory=dict)
    active_domain: str | None = None

    prev_router: str | None = None
    prev_politicas: str | None = None
    prev_gastos: str | None = None
    last_route: str | None = None
    last_domain_route: str | None = None  
    recent_user: list[str] = field(default_factory=list)
    sticky_route: str | None = None  
    sticky_ttl: int = 0              
    pending_route: str | None = None   
    pending_reason: str | None = None  
    last_assistant: str | None = None

class InMemorySessionStore:
    def __init__(self):
        self._db: dict[str, Session] = {}

    def get(self, conversation_id: str) -> Session:
        return self._db.setdefault(conversation_id, Session())

def remember_user(session: Session, user_text: str, max_items: int = 6) -> None:
    t = (user_text or "").strip()
    if not t:
        return
    session.recent_user.append(t)
    if len(session.recent_user) > max_items:
        session.recent_user = session.recent_user[-max_items:]

def should_use_sticky(session: Session, user_text: str) -> bool:
    
    return bool(session.sticky_route) and session.sticky_ttl > 0 and is_affirmation_or_short(user_text)

def consume_sticky(session: Session) -> None:
    if session.sticky_ttl > 0:
        session.sticky_ttl -= 1
    if session.sticky_ttl <= 0:
        session.sticky_route = None
        session.sticky_ttl = 0

def set_sticky(session: Session, route: str, ttl: int = 2) -> None:
    
    session.sticky_route = route
    session.sticky_ttl = ttl
    session.last_route = route
