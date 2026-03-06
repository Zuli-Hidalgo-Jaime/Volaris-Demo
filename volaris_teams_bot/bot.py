# bot.py
import os
import asyncio
from botbuilder.core import ActivityHandler, TurnContext, MessageFactory

from router import InMemorySessionStore, is_affirmation_or_short
from foundry_client import (
    AgentRef,
    FoundryConfig,
    FoundryHostedAgents,
    _infer_single_route,
    _looks_like_anticipo_question,
    _should_release_pending_route,
)


def _needs_expense_followup(answer: str) -> bool:
    s = (answer or "").strip().lower()
    return (
        "para guardar el gasto necesito:" in s
        or "responde exactamente: confirmo" in s
        or "quisiste decir:" in s
    )


def _is_rate_limited_answer(answer: str) -> bool:
    s = (answer or "").strip().lower()
    return "estamos recibiendo demasiadas solicitudes" in s


class VolarisBot(ActivityHandler):
    def __init__(self, store: InMemorySessionStore):
        self.store = store
        self.use_router_structured = os.getenv("ROUTER_STRUCTURED", "true").strip().lower() in {"1", "true", "yes", "on"}
        cfg = FoundryConfig(
            project_endpoint=os.environ["PROJECT_ENDPOINT"],
            router=AgentRef.from_env("ROUTER"),
            policies=AgentRef.from_env("POLICIES"),
            expenses=AgentRef.from_env("EXPENSES"),
        )
        self.foundry = FoundryHostedAgents(cfg)

    async def on_message_activity(self, turn_context: TurnContext):
        user_text = (turn_context.activity.text or "").strip()
        conversation_id = turn_context.activity.conversation.id

        session = self.store.get(conversation_id)
        social_turn = False
        routed_action = ""
        if not is_affirmation_or_short(user_text):
            session.recent_user.append(user_text)
            session.recent_user = session.recent_user[-3:]

        if (
            session.pending_route in {"gastos", "politicas"}
            and not _should_release_pending_route(user_text, session.pending_route)
        ):
            route = session.pending_route
        else:
            if session.pending_route in {"gastos", "politicas"}:
                session.pending_route = None
                session.pending_reason = None

            router_tid = session.router_thread_id or session.threads_by_domain.get("router")
            if self.use_router_structured:
                decision, session.prev_router = await asyncio.to_thread(
                    self.foundry.classify_route_structured,
                    user_text,
                    router_tid,
                    session.last_route,
                    session.recent_user,
                    session.last_assistant,
                )
                route = str(decision.get("route") or "conversacional")
                social_turn = (decision.get("speech_act") == "social")
                routed_action = str(decision.get("action") or "").strip().lower()
            else:
                route, session.prev_router = await asyncio.to_thread(
                    self.foundry.classify_route,
                    user_text,
                    router_tid,
                    session.last_route,
                    session.recent_user,
                    session.last_assistant,
                )
            session.router_thread_id = session.prev_router
            if session.prev_router:
                session.threads_by_domain["router"] = session.prev_router

        route = (route or "").strip().lower()
        if social_turn:
            route = "conversacional"

        if (
            self.use_router_structured
            and route == "gastos"
            and session.pending_route != "gastos"
            and routed_action not in {"registrar", "validar", "presupuesto"}
        ):
            route = "politicas"

        if (
            route == "conversacional"
            and session.sticky_route
            and session.sticky_ttl > 0
            and (any(ch.isdigit() for ch in user_text) or len(user_text) > 12)
            and not is_affirmation_or_short(user_text)
        ):
            inferred = _infer_single_route(user_text, session.last_route)
            if inferred in {"politicas", "gastos"}:
                route = inferred
            else:
                route = session.sticky_route
                session.sticky_ttl -= 1

        if route == "conversacional":
            answer = (
                "Hola. Puedo ayudarte con:\n"
                "1) Politicas/procedimientos (topes, plazos, comprobantes, que hacer si falta ticket)\n"
                "2) Gastos/presupuesto (consultar presupuesto, registrar o validar un gasto)\n"
                "¿qué necesitas?"
            )
            session.last_assistant = answer
            await turn_context.send_activity(MessageFactory.text(answer))
            return

        # si el texto habla de un anticipo con duda, mejor enviarlo a politicas
        # para que no intente adivinar un proyecto.
        if _looks_like_anticipo_question(user_text):
            route = "politicas"

        if route == "politicas":
            pol_tid = session.politicas_thread_id or session.threads_by_domain.get("politicas") or session.prev_politicas
            answer, session.prev_politicas = await asyncio.to_thread(
                self.foundry.ask_policies, user_text, pol_tid
            )
            session.politicas_thread_id = session.prev_politicas
            if session.prev_politicas:
                session.threads_by_domain["politicas"] = session.prev_politicas
            if not _is_rate_limited_answer(answer):
                session.last_route = "politicas"
                session.last_domain_route = "politicas"
                session.active_domain = "politicas"

        elif route == "gastos":
            gas_tid = session.gastos_thread_id or session.threads_by_domain.get("gastos") or session.prev_gastos
            answer, session.prev_gastos = await asyncio.to_thread(
                self.foundry.ask_expenses, user_text, gas_tid
            )
            session.gastos_thread_id = session.prev_gastos
            if session.prev_gastos:
                session.threads_by_domain["gastos"] = session.prev_gastos
            if _is_rate_limited_answer(answer):
                pass
            else:
                if isinstance(answer, str) and _needs_expense_followup(answer):
                    session.pending_route = "gastos"
                    session.pending_reason = "followup"
                else:
                    if session.pending_route == "gastos":
                        session.pending_route = None
                        session.pending_reason = None
                session.last_route = "gastos"
                session.last_domain_route = "gastos"
                session.active_domain = "gastos"

        else:  # collab
            pol_tid = session.politicas_thread_id or session.threads_by_domain.get("politicas") or session.prev_politicas
            pol, session.prev_politicas = await asyncio.to_thread(
                self.foundry.ask_policies, user_text, pol_tid
            )
            session.politicas_thread_id = session.prev_politicas
            if session.prev_politicas:
                session.threads_by_domain["politicas"] = session.prev_politicas

            gas_tid = session.gastos_thread_id or session.threads_by_domain.get("gastos") or session.prev_gastos
            gas, session.prev_gastos = await asyncio.to_thread(
                self.foundry.ask_expenses, user_text, gas_tid
            )
            session.gastos_thread_id = session.prev_gastos
            if session.prev_gastos:
                session.threads_by_domain["gastos"] = session.prev_gastos
            answer = f"Segun politicas:\n{pol}\n\nPara registrar/validar el gasto:\n{gas}"
            if not _is_rate_limited_answer(answer):
                session.last_route = "gastos"
                session.last_domain_route = "gastos"
                session.active_domain = "gastos"

        if route in {"politicas", "gastos", "collab"} and not _is_rate_limited_answer(answer):
            session.sticky_route = "gastos" if route == "collab" else route
            session.sticky_ttl = 3

        session.last_assistant = answer
        await turn_context.send_activity(MessageFactory.text(answer))
