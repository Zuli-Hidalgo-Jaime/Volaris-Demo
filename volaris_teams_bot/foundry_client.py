# foundry_client.py
from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

import openai
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentReference


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _extract_route_label(raw_output: str) -> str | None:
    normalized = unicodedata.normalize("NFD", (raw_output or "").lower())
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"[^a-z]+", " ", normalized)
    for token in normalized.split():
        if token in {"politicas", "gastos", "collab", "conversacional"}:
            return token
    return None


def _clean_previous_response_id(thread_id: Optional[str]) -> str:
    if isinstance(thread_id, str) and thread_id.startswith("resp"):
        return thread_id
    return ""


def _extract_router_json(raw_output: str) -> dict:
    raw = (raw_output or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {}

    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return {}
    return {}


def _normalize_route(route: str | None) -> str:
    r = (route or "").strip().lower()
    if r in {"politicas", "gastos", "collab", "conversacional"}:
        return r
    return ""


def _normalize_speech_act(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"task", "social"}:
        return v
    return ""


def _is_explicit_collab_request(user_text: str) -> bool:
    t = unicodedata.normalize("NFD", (user_text or "").lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = f" {t} "

    policy_terms = (
        " politica", " politicas", " regla", " reglas",
        " lineamiento", " lineamientos", " tope", " topes", " procedimiento",
    )
    expense_action_terms = (
        " registrar", " registra", " registro", " validar", " validacion",
        " proyecto", " proyectos", " presupuesto",
        " cargar", " sube", " subir", " reportar", " capturar",
    )

    policy_like = _contains_any(t, policy_terms)
    expense_action_like = _contains_any(t, expense_action_terms)
    if not (policy_like and expense_action_like):
        return False

    explicit_joiners = (
        " despues ", " luego ", " ademas ",
        " tambien ", " y luego ", " primero ", " segundo ",
        " al mismo tiempo ", ";", "\n",
    )
    if any(joiner in t for joiner in explicit_joiners):
        return True

    return bool(re.search(r"\b(y|e)\s+(registra|valida|carga|sube|aplica|ejecuta|crea)\b", t))


def _looks_like_affirmation_or_short_for_pending(user_text: str) -> bool:
    t = unicodedata.normalize("NFD", (user_text or "").lower().strip())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = t.strip("¿?¡!.,;:()[]{}\"'` ")
    t = re.sub(r"\s+", " ", t)

    if not t:
        return False

    if t in {"si", "ok", "va", "dale", "de acuerdo", "confirmo", "aja"}:
        return True
    if re.fullmatch(r"(y\s+)?por\s+\d+(\.\d+)?", t):
        return True
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return True
    return False


def _should_release_pending_route(user_text: str, pending_route: str | None) -> bool:
    pending = (pending_route or "").strip().lower()
    if pending not in {"gastos", "politicas"}:
        return False

    raw = (user_text or "").strip()
    if not raw:
        return False

    # Confirmaciones cortas mantienen el flujo pendiente.
    if _looks_like_affirmation_or_short_for_pending(raw):
        return False

    normalized = unicodedata.normalize("NFD", raw.lower())
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"\s+", " ", normalized)

    escape_phrases = (
        "cambiar de tema",
        "cambia de tema",
        "otro tema",
        "nueva pregunta",
        "nueva cosa",
        "olvida eso",
        "deja eso",
        "cancelar",
        "cancela",
        "mejor otra cosa",
        "ya no",
        "deten",
        "detente",
    )
    if any(phrase in normalized for phrase in escape_phrases):
        return True

    # Si el turno actual apunta claramente al otro dominio, libera el pending.
    inferred = _infer_single_route(raw, None)
    return inferred in {"gastos", "politicas"} and inferred != pending


def _looks_like_anticipo_question(text: str) -> bool:
    """Heurística simple para detectar consultas sobre anticipos.

    Si el usuario menciona "anticipo" y parece formular una pregunta o duda,
    asumimos que es un tema de políticas en lugar de un registro de gasto.
    """
    s = (text or "").lower()
    if "anticipo" not in s:
        return False
    # si hay signo de interrogación o palabras típicas de pregunta
    if "?" in text or "qué" in s or "que" in s or "cómo" in s or "como" in s:
        return True
    return False


def _infer_single_route(user_text: str, last_route: str | None) -> str:
    raw = user_text or ""
    # override early for anticipo queries
    if _looks_like_anticipo_question(raw):
        return "politicas"

    t = unicodedata.normalize("NFD", raw.lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    padded = f" {t} "

    expense_action_terms = (
        " registrar", " registra", " registro", " validar", " validacion",
        " proyecto", " proyectos", " presupuesto",
        " cargar", " sube", " subir", " saldo", " disponible",
        " reportar", " capturar",
    )
    policy_terms = (
        " politica", " politicas", " regla", " reglas", " lineamiento",
        " lineamientos", " tope", " topes", " procedimiento",
        " permitido", " permitidos", " autoriza", " autorizados",
        " aprobable", " aprobables", " viatico", " viaticos",
    )
    policy_scope_terms = (
        " que tipo de gasto", " que tipo de gastos", " que gastos",
        " cuales gastos", " cuales son los gastos", " gastos de viaje",
        " reconoce", " permite", " permitidos", " autorizados",
        " aprobables", " viaticos", " viatico",
        " ticket", " tickets", " comprobante", " comprobantes",
        " factura", " facturas", " evidencia", " evidencias",
        " perdi", " pierdo", " extravie", " extravio", " sin ticket",
        # anticipos y adelantos también son parte de la política de gastos
        " anticipo", " adelanto",
    )

    question_terms = (" que ", " cual ", " cuales ", " como ", " cuanto ", " cuando ")
    has_question_intent = ("?" in raw) or _contains_any(padded, question_terms) or bool(
        re.match(r"^\s*(que|cual|cuales|como|cuanto|cuando)\b", t)
    )
    has_expense_action = _contains_any(padded, expense_action_terms)

    report_terms = (
        " reporte", " reportes", " informe", " informes",
        " comprobacion", " comprobar", " rendicion",
    )
    deadline_terms = (
        " tiempo", " plazo", " plazos", " dia", " dias", " fecha limite",
        " cuando vence", " hasta cuando", " dentro de",
    )
    asks_deadline_for_report = has_question_intent and _contains_any(padded, report_terms) and _contains_any(padded, deadline_terms)

    # Preguntas de plazo/tiempo para subir o comprobar reportes son de política.
    if asks_deadline_for_report:
        return "politicas"

    asks_policy_scope = has_question_intent and (
        _contains_any(padded, policy_scope_terms)
        or (
            _contains_any(padded, (" gasto", " gastos", " viatico", " viaticos"))
            and not has_expense_action
        )
    )

    # Preguntas tipo "que gastos reconoce/permite" van a politicas.
    if asks_policy_scope:
        return "politicas"

    evidence_terms = (
        " ticket", " tickets", " comprobante", " comprobantes",
        " factura", " facturas", " evidencia", " evidencias",
    )
    loss_terms = (" perdi", " pierdo", " extravie", " extravio", " sin ")

    # Dudas sobre evidencia/tickets perdidos son de politica (no de registro operativo).
    if _contains_any(padded, evidence_terms) and (has_question_intent or _contains_any(padded, loss_terms)):
        return "politicas"

    # Menciones claras de politicas sin accion operativa.
    if _contains_any(padded, policy_terms) and not has_expense_action:
        return "politicas"

    # Acciones operativas o consultas de estado de presupuesto => gastos.
    if has_expense_action:
        return "gastos"

    # Si solo menciona gasto(s) y no hay accion, pregunta => politicas; enunciado => gastos.
    has_expense_noun = _contains_any(padded, (" gasto", " gastos", " viatico", " viaticos"))
    if has_expense_noun and has_question_intent:
        return "politicas"
    if has_expense_noun:
        return "gastos"

    if last_route in {"politicas", "gastos"}:
        return last_route
    return "conversacional"



@dataclass(frozen=True)
class AgentRef:
    name: str
    version: Optional[str] = None  # None = latest

    @staticmethod
    def from_env(prefix: str) -> "AgentRef":
        """
        Soporta:
        - {prefix}_AGENT_ID = "Name:Version"
        o
        - {prefix}_AGENT_NAME y {prefix}_AGENT_VERSION
        """
        agent_id = os.getenv(f"{prefix}_AGENT_ID")
        if agent_id and ":" in agent_id:
            name, version = agent_id.split(":", 1)
            return AgentRef(name=name.strip(), version=version.strip())

        name = os.getenv(f"{prefix}_AGENT_NAME")
        if not name:
            raise ValueError(f"Falta {prefix}_AGENT_ID o {prefix}_AGENT_NAME en .env")
        version = os.getenv(f"{prefix}_AGENT_VERSION")
        return AgentRef(name=name.strip(), version=(version.strip() if version else None))


@dataclass(frozen=True)
class FoundryConfig:
    project_endpoint: str
    router: AgentRef
    policies: AgentRef
    expenses: AgentRef


class FoundryHostedAgents:
    """
    Hosted Agents (Name/Version) invocados con OpenAI Responses + extra_body agent reference.
    Para continuidad por dominio, cada agente mantiene su propia cadena via previous_response_id.
    """

    def __init__(self, cfg: FoundryConfig):
        self.cfg = cfg
        self.credential = DefaultAzureCredential()
        self.project = AIProjectClient(endpoint=cfg.project_endpoint, credential=self.credential)

    def close(self):
        try:
            self.project.close()
        finally:
            self.credential.close()

    def _agent_extra_body(self, agent: AgentRef) -> dict:
        return {"agent": AgentReference(name=agent.name, version=agent.version).as_dict()}

    def _call_agent(
        self,
        agent: AgentRef,
        user_text: str,
        thread_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Devuelve (output_text, thread_id).
        Nota: en este cliente "thread_id" es el response_id previo de OpenAI Responses.
        """
        max_retries = 6
        base_delay = 0.6
        max_delay = 8.0
        thread_id = _clean_previous_response_id(thread_id)

        kwargs = {
            "input": [{"role": "user", "content": user_text}],
            "extra_body": self._agent_extra_body(agent),
        }
        if thread_id:
            kwargs["previous_response_id"] = thread_id

        for attempt in range(max_retries + 1):
            try:
                with self.project.get_openai_client() as openai_client:
                    resp = openai_client.responses.create(**kwargs)
                    return (resp.output_text or ""), resp.id
            except openai.RateLimitError as e:
                if attempt >= max_retries:
                    return (
                        "Estamos recibiendo demasiadas solicitudes en este momento. Intenta de nuevo en unos segundos.",
                        thread_id or "",
                    )

                retry_after = None
                try:
                    headers = None
                    if getattr(e, "response", None) is not None:
                        headers = getattr(e.response, "headers", None)
                    if headers is None:
                        headers = getattr(e, "headers", None)
                    if headers:
                        ra = headers.get("Retry-After") or headers.get("retry-after")
                        if ra is not None:
                            retry_after = float(ra)
                except Exception:
                    retry_after = None

                if retry_after is not None and retry_after >= 0:
                    delay = retry_after + random.uniform(0, 0.25)
                else:
                    delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, 0.25)

                time.sleep(delay)
            except openai.BadRequestError as e:
                # Si quedó un previous_response_id inválido en memoria, reintenta sin arrastrarlo.
                if "previous_response_id" in str(e) and kwargs.get("previous_response_id"):
                    kwargs.pop("previous_response_id", None)
                    thread_id = ""
                    continue
                raise

    # --- Calls por agente ---
    def ask_policies(self, user_text: str, thread_id: Optional[str]) -> Tuple[str, str]:
        return self._call_agent(self.cfg.policies, user_text, thread_id)

    def ask_expenses(self, user_text: str, thread_id: Optional[str]) -> Tuple[str, str]:
        """Preguntas sobre gastos operativos.

        Normalmente delega en el agente de gastos, pero si la consulta parece ser
        una duda acerca de un anticipo o adelanto (p.ej. "no me gasté todo el
        anticipo" o "¿el anticipo también se comprueba?"), reenvía a políticas
        para evitar que el bot intente reconocer un proyecto.
        """
        if _looks_like_anticipo_question(user_text):
            # Reutiliza el thread disponible para no perder continuidad.
            return self.ask_policies(user_text, thread_id)
        return self._call_agent(self.cfg.expenses, user_text, thread_id)

    def classify_route_structured(
        self,
        user_text: str,
        thread_id: str | None,
        last_route: str | None,
        recent_user: list[str],
        last_assistant: str | None = None,
    ) -> tuple[dict, str]:
        history = "\n".join([f"- {m}" for m in recent_user[-3:]]) or "- (vacio)"
        last = last_route or "(none)"
        assistant_ctx = (last_assistant or "").strip()
        if assistant_ctx:
            assistant_ctx = assistant_ctx[:400]

        prompt = (
            "Eres un ROUTER. Responde SOLO JSON compacto con este schema:\n"
            "{\"route\":\"politicas|gastos|collab|conversacional\",\"speech_act\":\"task|social\",\"action\":\"none|registrar|validar|presupuesto|politica|otro\"}\n"
            "Sin markdown, sin texto adicional.\n\n"
            f"Ruta anterior: {last}\n"
            f"Ultimos mensajes del usuario (max 3):\n{history}\n\n"
            f"Ultimo mensaje del asistente (recortado): {assistant_ctx or '(vacio)'}\n\n"
            f"Mensaje actual: {user_text}\n\n"
            "Reglas:\n"
            "1) Si es saludo/cortesia/agradecimiento, route=conversacional, speech_act=social, action=none.\n"
            "2) Si es solo consulta de politica/reglas/procedimiento, route=politicas.\n"
            "3) Si es accion operativa de gastos (registrar/validar/proyecto/presupuesto/saldo), route=gastos.\n"
            "4) route=collab solo si pide politicas y accion de gastos en el mismo mensaje."
        )

        out, new_prev = self._call_agent(self.cfg.router, prompt, thread_id)
        parsed = _extract_router_json(out)

        label = _normalize_route(parsed.get("route")) or _extract_route_label(out or "") or "conversacional"
        speech_act = _normalize_speech_act(parsed.get("speech_act"))
        action = (str(parsed.get("action", "")).strip().lower() or "otro")

        explicit_collab = _is_explicit_collab_request(user_text)
        inferred = _infer_single_route(user_text, last_route)

        if explicit_collab:
            label = "collab"
        elif label == "collab":
            label = inferred

        if label not in {"politicas", "gastos", "collab", "conversacional"}:
            label = inferred

        # Fallback solo si el router quedó conversacional y inferred sí ve dominio.
        if label == "conversacional" and inferred in {"politicas", "gastos"}:
            label = inferred

        if not speech_act:
            speech_act = "social" if label == "conversacional" else "task"

        if speech_act == "social":
            label = "conversacional"
            action = "none"

        return (
            {
                "route": label,
                "speech_act": speech_act,
                "action": action,
            },
            new_prev,
        )

    def classify_route(
        self,
        user_text: str,
        thread_id: str | None,
        last_route: str | None,
        recent_user: list[str],
        last_assistant: str | None = None,
    ) -> tuple[str, str]:
        decision, new_prev = self.classify_route_structured(
            user_text=user_text,
            thread_id=thread_id,
            last_route=last_route,
            recent_user=recent_user,
            last_assistant=last_assistant,
        )
        return str(decision.get("route") or "conversacional"), new_prev
