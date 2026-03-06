# local_chat.py
from dotenv import load_dotenv
load_dotenv()

import os
import re
from router import (
    InMemorySessionStore,
    is_affirmation_or_short,
    remember_user,
    set_sticky,
)
from foundry_client import (
    AgentRef,
    FoundryConfig,
    FoundryHostedAgents,
    _infer_single_route,
    _looks_like_anticipo_question,
    _should_release_pending_route,
)


def _is_greeting_or_courtesy(user_text: str) -> bool:
    s = (user_text or "").strip().lower()
    s = s.strip("¿?¡!.,;:()[]{}\"' ")
    return s in {
        "hola", "holi", "buenas", "buenos dias", "buen dia", "buenas tardes",
        "buenas noches", "hello", "hi", "gracias", "muchas gracias",
    }


def _looks_like_budget_query(user_text: str) -> bool:
    s = (user_text or "").strip().lower()
    return "presupuesto" in s


def _looks_like_followup(user_text: str) -> bool:
    s = (user_text or "").strip().lower()
    if not s:
        return False
   
    s = s.lstrip("¿?¡!.,;:()[]{}\"'")
    
    starters = (
        "y ", "y para", "y del", "pero", "entonces", "sobre", "del ", "de ", "para "
    )
    return s.startswith(starters)


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


def is_short_followup(t: str) -> bool:
    s = (t or "").strip().lower()
    if _is_greeting_or_courtesy(s):
        return False
    if is_affirmation_or_short(s):
        return True
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return True
    if re.fullmatch(r"(y\s+)?por\s+\d+(\.\d+)?", s):
        return True
    return (len(s) <= 12) and ("?" not in s) and ("¿" not in s)


def main():
    cfg = FoundryConfig(
        project_endpoint=os.environ["PROJECT_ENDPOINT"],
        router=AgentRef.from_env("ROUTER"),
        policies=AgentRef.from_env("POLICIES"),
        expenses=AgentRef.from_env("EXPENSES"),
    )
    foundry = FoundryHostedAgents(cfg)
    store = InMemorySessionStore()
    use_router_structured = os.getenv("ROUTER_STRUCTURED", "true").strip().lower() in {"1", "true", "yes", "on"}

    conv_id = "local-demo"

    print("Escribe y presiona Enter. 'salir' para terminar.\n")

    try:
        while True:
            user_text = input("Tu: ").strip()

            if user_text.lower() in {"salir", "exit", "quit"}:
                break

            # Evita turnos vacíos (esto te estaba re-disparando last_route)
            if not user_text:
                continue

            session = store.get(conv_id)
            social_turn = False
            routed_action = ""

            # Si estamos en modo aclaración, NO rutear: sigue con el agente pendiente
            if (
                session.pending_route in {"gastos", "politicas"}
                and not _should_release_pending_route(user_text, session.pending_route)
            ):
                route = session.pending_route
                if not is_affirmation_or_short(user_text):
                    remember_user(session, user_text, max_items=3)
            else:
                if session.pending_route in {"gastos", "politicas"}:
                    session.pending_route = None
                    session.pending_reason = None

                # Guarda contexto solo si NO es confirmación corta
                if not is_affirmation_or_short(user_text):
                    remember_user(session, user_text, max_items=3)

                followup = is_short_followup(user_text)
                ctx_last = session.last_route if followup else None
                ctx_recent = session.recent_user if followup else []
                router_tid = session.router_thread_id or session.threads_by_domain.get("router")

                if use_router_structured:
                    decision, session.prev_router = foundry.classify_route_structured(
                        user_text=user_text,
                        thread_id=router_tid,
                        last_route=ctx_last,
                        recent_user=ctx_recent,
                        last_assistant=session.last_assistant,
                    )
                    route = str(decision.get("route") or "conversacional")
                    social_turn = (decision.get("speech_act") == "social")
                    routed_action = str(decision.get("action") or "").strip().lower()
                else:
                    route, session.prev_router = foundry.classify_route(
                        user_text=user_text,
                        thread_id=router_tid,
                        last_route=ctx_last,
                        recent_user=ctx_recent,
                        last_assistant=session.last_assistant,
                    )

                session.router_thread_id = session.prev_router
                if session.prev_router:
                    session.threads_by_domain["router"] = session.prev_router

            route = (route or "").strip().lower()
            if social_turn:
                route = "conversacional"

            # Gate de seguridad: solo tratamos "gastos" como operativo
            # cuando el router estructurado marcó una acción operativa clara.
            if (
                use_router_structured
                and route == "gastos"
                and session.pending_route != "gastos"
                and routed_action not in {"registrar", "validar", "presupuesto"}
            ):
                route = "politicas"

            # Heuristica minima: consultas de presupuesto deben ir a gastos
            if route == "conversacional" and _looks_like_budget_query(user_text):
                route = "gastos"

            # Continuidad de dominio: si el router no está seguro pero el usuario hace follow-up,
            
            if route == "conversacional" and session.last_domain_route and _looks_like_followup(user_text):
                inferred = _infer_single_route(user_text, session.last_route)
                if inferred in {"politicas", "gastos"}:
                    route = inferred
                else:
                    route = session.last_domain_route

            # 2) Conversacional => no llamamos dominio
            if route == "conversacional":
                answer = (
                    "Hola. Puedo ayudarte con:\n"
                    "1) Politicas/procedimientos (topes, plazos, comprobantes, que hacer si falta ticket)\n"
                    "2) Gastos/presupuesto (consultar presupuesto, registrar o validar un gasto)\n"
                    "¿Qué necesitas?"
                )
                session.last_assistant = answer
                print(f"\nBot:\n{answer}\n")
                continue

            # 3) Enrutamiento
            # si hay mención de anticipo/duda, forzamos a políticas incluso si el
            # router devolvió otra cosa. Esto evita el caso donde el agente de
            # gastos intenta adivinar un proyecto y pide disambiguación.
            if _looks_like_anticipo_question(user_text):
                route = "politicas"

            if route == "politicas":
                pol_tid = session.politicas_thread_id or session.threads_by_domain.get("politicas") or session.prev_politicas
                answer, session.prev_politicas = foundry.ask_policies(user_text, pol_tid)
                session.politicas_thread_id = session.prev_politicas
                if session.prev_politicas:
                    session.threads_by_domain["politicas"] = session.prev_politicas
                if not _is_rate_limited_answer(answer):
                    session.last_route = "politicas"
                    session.last_domain_route = "politicas"
                    session.active_domain = "politicas"

            elif route == "gastos":
                gas_tid = session.gastos_thread_id or session.threads_by_domain.get("gastos") or session.prev_gastos
                answer, session.prev_gastos = foundry.ask_expenses(user_text, gas_tid)
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
                pol, session.prev_politicas = foundry.ask_policies(user_text, pol_tid)
                session.politicas_thread_id = session.prev_politicas
                if session.prev_politicas:
                    session.threads_by_domain["politicas"] = session.prev_politicas

                gas_tid = session.gastos_thread_id or session.threads_by_domain.get("gastos") or session.prev_gastos
                gas, session.prev_gastos = foundry.ask_expenses(user_text, gas_tid)
                session.gastos_thread_id = session.prev_gastos
                if session.prev_gastos:
                    session.threads_by_domain["gastos"] = session.prev_gastos
                answer = f"Segun politicas:\n{pol}\n\nPara registrar/validar el gasto:\n{gas}"
                if not _is_rate_limited_answer(answer):
                    session.last_route = "gastos"
                    session.last_domain_route = "gastos"
                    session.active_domain = "gastos"

            
            if route in {"politicas", "gastos", "collab"} and not _is_rate_limited_answer(answer):
                sticky = "gastos" if route == "collab" else route
                set_sticky(session, sticky, ttl=3)

            session.last_assistant = answer
            print(f"\nBot:\n{answer}\n")

    finally:
        foundry.close()


if __name__ == "__main__":
    main()
