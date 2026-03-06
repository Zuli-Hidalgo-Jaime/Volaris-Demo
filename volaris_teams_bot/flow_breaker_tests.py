from __future__ import annotations

import unittest

from bot import _needs_expense_followup
from foundry_client import (
    _extract_router_json,
    _infer_single_route,
    _is_explicit_collab_request,
    _looks_like_anticipo_question,
    _normalize_route,
    _normalize_speech_act,
    _should_release_pending_route,
)
from router import is_affirmation_or_short


def resolve_route_like_bot(
    *,
    route: str,
    social_turn: bool,
    routed_action: str,
    use_router_structured: bool,
    pending_route: str | None,
    sticky_route: str | None,
    sticky_ttl: int,
    user_text: str,
    last_route: str | None = None,
) -> str:
    """Replica la logica de guardrails en bot.py sin llamar a Azure."""
    pending_locked = (
        pending_route in {"gastos", "politicas"}
        and not _should_release_pending_route(user_text, pending_route)
    )
    if pending_locked:
        result = str(pending_route)
    else:
        result = (route or "").strip().lower()

        if social_turn:
            result = "conversacional"

    if (
        use_router_structured
        and result == "gastos"
        and pending_route != "gastos"
        and (routed_action or "").strip().lower() not in {"registrar", "validar", "presupuesto"}
    ):
        result = "politicas"

    if (
        result == "conversacional"
        and sticky_route
        and sticky_ttl > 0
        and (any(ch.isdigit() for ch in user_text) or len(user_text) > 12)
        and not is_affirmation_or_short(user_text)
    ):
        inferred = _infer_single_route(user_text, last_route)
        if inferred in {"politicas", "gastos"}:
            result = inferred
        else:
            result = sticky_route

    if _looks_like_anticipo_question(user_text):
        result = "politicas"

    return result


class RouteHeuristicsTests(unittest.TestCase):
    def test_policy_scope_question_goes_to_politicas(self) -> None:
        route = _infer_single_route("Que gastos reconoce la politica de viaticos?", last_route=None)
        self.assertEqual(route, "politicas")

    def test_operational_expense_action_goes_to_gastos(self) -> None:
        route = _infer_single_route("Registra un gasto de taxi por 230 en el proyecto Orion.", last_route=None)
        self.assertEqual(route, "gastos")

    def test_anticipo_question_goes_to_politicas(self) -> None:
        route = _infer_single_route("No me gaste todo el anticipo, que hago?", last_route="gastos")
        self.assertEqual(route, "politicas")

    def test_plain_greeting_defaults_to_conversacional(self) -> None:
        route = _infer_single_route("hola", last_route=None)
        self.assertEqual(route, "conversacional")

    def test_explicit_collab_request_detected(self) -> None:
        text = "Dime la politica de comprobacion y luego registra el gasto de hotel."
        self.assertTrue(_is_explicit_collab_request(text))

    def test_non_collab_request_not_detected(self) -> None:
        self.assertFalse(_is_explicit_collab_request("Cual es la politica de viaticos?"))


class RouterParsingTests(unittest.TestCase):
    def test_extracts_clean_json(self) -> None:
        parsed = _extract_router_json('{"route":"gastos","speech_act":"task","action":"registrar"}')
        self.assertEqual(parsed.get("route"), "gastos")

    def test_extracts_json_embedded_in_text(self) -> None:
        raw = "```json\n{\"route\":\"politicas\",\"speech_act\":\"task\",\"action\":\"politica\"}\n```"
        parsed = _extract_router_json(raw)
        self.assertEqual(parsed.get("route"), "politicas")

    def test_invalid_router_output_returns_empty_dict(self) -> None:
        self.assertEqual(_extract_router_json("route=politicas"), {})

    def test_normalizers(self) -> None:
        self.assertEqual(_normalize_route("GaStOs"), "gastos")
        self.assertEqual(_normalize_route("otro"), "")
        self.assertEqual(_normalize_speech_act("SOCIAL"), "social")
        self.assertEqual(_normalize_speech_act("none"), "")


class GuardrailFlowTests(unittest.TestCase):
    def test_social_turn_forces_conversational(self) -> None:
        out = resolve_route_like_bot(
            route="gastos",
            social_turn=True,
            routed_action="registrar",
            use_router_structured=True,
            pending_route=None,
            sticky_route=None,
            sticky_ttl=0,
            user_text="gracias",
        )
        self.assertEqual(out, "conversacional")

    def test_gastos_without_operational_action_is_downgraded(self) -> None:
        out = resolve_route_like_bot(
            route="gastos",
            social_turn=False,
            routed_action="otro",
            use_router_structured=True,
            pending_route=None,
            sticky_route=None,
            sticky_ttl=0,
            user_text="que gastos reconoce la politica?",
        )
        self.assertEqual(out, "politicas")

    def test_pending_gastos_keeps_gastos_even_without_action(self) -> None:
        out = resolve_route_like_bot(
            route="gastos",
            social_turn=False,
            routed_action="otro",
            use_router_structured=True,
            pending_route="gastos",
            sticky_route=None,
            sticky_ttl=0,
            user_text="si",
        )
        self.assertEqual(out, "gastos")

    def test_pending_gastos_releases_on_explicit_topic_change(self) -> None:
        out = resolve_route_like_bot(
            route="conversacional",
            social_turn=False,
            routed_action="none",
            use_router_structured=True,
            pending_route="gastos",
            sticky_route=None,
            sticky_ttl=0,
            user_text="Cambiando de tema, cual es la politica de anticipos?",
        )
        self.assertEqual(out, "politicas")

    def test_pending_gastos_releases_on_clear_policy_intent(self) -> None:
        out = resolve_route_like_bot(
            route="politicas",
            social_turn=False,
            routed_action="none",
            use_router_structured=True,
            pending_route="gastos",
            sticky_route=None,
            sticky_ttl=0,
            user_text="Cual es el tope de hotel segun la politica?",
        )
        self.assertEqual(out, "politicas")

    def test_sticky_route_rescues_context_from_conversational(self) -> None:
        out = resolve_route_like_bot(
            route="conversacional",
            social_turn=False,
            routed_action="none",
            use_router_structured=True,
            pending_route=None,
            sticky_route="gastos",
            sticky_ttl=3,
            user_text="en proyecto OR-12 por 950",
        )
        self.assertEqual(out, "gastos")

    def test_sticky_does_not_override_clear_policy_question(self) -> None:
        out = resolve_route_like_bot(
            route="conversacional",
            social_turn=False,
            routed_action="none",
            use_router_structured=True,
            pending_route=None,
            sticky_route="gastos",
            sticky_ttl=3,
            user_text="Cuales son los topes para hotel?",
            last_route="gastos",
        )
        self.assertEqual(out, "politicas")

    def test_anticipo_override_wins_even_if_gastos(self) -> None:
        out = resolve_route_like_bot(
            route="gastos",
            social_turn=False,
            routed_action="registrar",
            use_router_structured=True,
            pending_route=None,
            sticky_route=None,
            sticky_ttl=0,
            user_text="El anticipo tambien se comprueba?",
        )
        self.assertEqual(out, "politicas")


class FollowupLockTests(unittest.TestCase):
    def test_followup_phrase_is_detected(self) -> None:
        self.assertTrue(_needs_expense_followup("Para guardar el gasto necesito: proyecto, monto y fecha."))
        self.assertTrue(_needs_expense_followup("Responde exactamente: confirmo"))
        self.assertTrue(_needs_expense_followup("Quisiste decir: Orion o Orion?"))

    def test_regular_answer_is_not_followup_lock(self) -> None:
        self.assertFalse(_needs_expense_followup("Tu gasto fue registrado correctamente."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
