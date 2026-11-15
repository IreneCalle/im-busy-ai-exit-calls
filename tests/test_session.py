"""CallSession: the five-turn arc with a fake model."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from imbusy.call.session import (
    CALL_CONNECTED,
    CallSession,
    CallState,
    InvalidTransition,
    arc_for,
    check_transition,
)
from imbusy.excuse.schema import BASE_FORBIDDEN, CallScript
from imbusy.llm import FakeLLM, LLMError, ModelRole
from imbusy.prompts import load_prompt, load_yaml_prompt
from imbusy.safety.safeword import SafeWordMatcher, parse_safe_word

REPLIES = [
    "Es que me he dejado las llaves dentro.",
    "Y el cerrajero no viene hasta las nueve.",
    "¿Me traes las de repuesto?",
    "Gracias, cariño. Te espero abajo. Un beso.",
]


async def say(session: CallSession, text: str) -> str:
    return "".join([chunk async for chunk in session.respond(text)])


def make_session(script: CallScript, llm: FakeLLM) -> CallSession:
    return CallSession(call_id="c1", script=script, llm=llm)


@pytest.mark.parametrize("turns", [3, 4, 5, 6, 8])
def test_arc_length_matches_turn_budget_and_ends_closing(turns: int) -> None:
    arc = arc_for(turns)
    assert len(arc) == turns
    assert arc[0] is CallState.OPENING
    assert arc[-1] is CallState.CLOSING
    for current, nxt in itertools.pairwise(arc):
        check_transition(current, nxt)


def test_arc_rejects_too_few_turns() -> None:
    with pytest.raises(ValueError):
        arc_for(2)


def test_no_way_back_from_closing() -> None:
    with pytest.raises(InvalidTransition):
        check_transition(CallState.CLOSING, CallState.ASK)
    check_transition(CallState.CLOSING, CallState.ENDED)


def test_persona_and_directive_prompts_load() -> None:
    persona = load_prompt("imbusy.call", "caller.md")
    assert "$turn_directive" in persona.body
    _, turns = load_yaml_prompt("imbusy.call", "turns.yaml")
    for state in ("hook", "escalate", "ask", "closing", "nudge"):
        assert set(turns["directives"][state]) == {"es-ES", "en-US"}


def test_opening_line_is_the_script_opening_and_needs_no_model_call(script: CallScript) -> None:
    llm = FakeLLM()
    session = make_session(script, llm)
    assert session.state is CallState.RINGING
    assert session.opening_line() == script.opening_line
    assert session.state is CallState.OPENING
    assert llm.calls == []
    assert session.turn_index == 1


async def test_states_advance_one_per_user_turn(script: CallScript) -> None:
    """After the greeting, four user prompts drive HOOK, ESCALATE, ASK, CLOSING."""
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    seen = []
    for user_text, reply in zip(["¿Sí?", "¿Y eso?", "Vaya.", "Vale, voy."], REPLIES, strict=True):
        assert await say(session, user_text) == reply
        seen.append(session.state)
    assert seen == [CallState.HOOK, CallState.ESCALATE, CallState.ASK, CallState.CLOSING]
    assert all(call.role is ModelRole.FAST for call in llm.calls)
    assert session.turn_index == 5


async def test_speech_after_farewell_is_ignored(script: CallScript) -> None:
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    for text in ["a", "b", "c", "d"]:
        await say(session, text)
    assert await say(session, "¿Oye?") == ""
    assert len(llm.calls) == 4


async def test_system_prompt_contains_state_directive_and_forbidden_rules(
    script: CallScript,
) -> None:
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    await say(session, "¿Sí?")
    system = llm.calls[0].system
    assert "Tía Carmen" in system
    assert "Say why you are calling" in system
    assert all(rule in system for rule in BASE_FORBIDDEN)
    assert "$" not in system
    for _ in range(3):
        await say(session, "vale")
    assert "This is your last turn" in llm.calls[-1].system


async def test_history_alternates_and_starts_with_user(script: CallScript) -> None:
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    await say(session, "¿Sí?")
    messages = llm.calls[0].messages
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[0].content == CALL_CONNECTED
    assert messages[1].content == script.opening_line
    assert messages[2].content == "¿Sí?"


async def test_closing_turn_is_flagged_final(script: CallScript) -> None:
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    for text in ["a", "b", "c"]:
        await say(session, text)
        assert not session.is_final_turn
    await say(session, "d")
    assert session.is_final_turn


async def test_interrupted_turn_is_truncated_in_history(script: CallScript) -> None:
    """After mark_interrupted, history shows only the heard words for that turn."""
    llm = FakeLLM(text_replies=list(REPLIES))
    session = make_session(script, llm)
    session.opening_line()
    await say(session, "¿Sí?")
    session.mark_interrupted("Es que me han")
    await say(session, "¿Qué te han hecho?")
    history = llm.calls[1].messages
    assert history[-2].content == "Es que me han"
    assert session.turns[1].interrupted


async def test_refusal_mid_call_speaks_fallback_line_and_closes(script: CallScript) -> None:
    """A model refusal yields the language's fallback line and moves to CLOSING."""
    llm = FakeLLM(text_replies=[FakeLLM.REFUSAL])
    session = make_session(script, llm)
    session.opening_line()
    spoken = await say(session, "¿Sí?")
    assert spoken == session.fallback_line()
    assert "te dejo" in spoken
    assert session.is_final_turn
    assert session.turns[-1].fallback


async def test_llm_error_also_falls_back(script: CallScript) -> None:
    class Broken(FakeLLM):
        async def stream_text(self, **kwargs):  # type: ignore[no-untyped-def,override]
            raise LLMError("boom")
            yield  # pragma: no cover

    session = make_session(script, Broken())
    session.opening_line()
    assert await say(session, "¿Sí?") == session.fallback_line()
    assert session.is_final_turn


async def test_markup_is_not_spoken(script: CallScript) -> None:
    session = make_session(script, FakeLLM(text_replies=["*suspira* Bueno, oye."]))
    session.opening_line()
    assert await say(session, "¿Sí?") == "suspira Bueno, oye."


async def test_nudge_does_not_advance_the_arc(script: CallScript) -> None:
    llm = FakeLLM(text_replies=["¿Hola? ¿Me oyes?", REPLIES[0]])
    session = make_session(script, llm)
    session.opening_line()
    nudge = "".join([c async for c in session.nudge()])
    assert nudge == "¿Hola? ¿Me oyes?"
    assert session.state is CallState.OPENING
    assert "said nothing" in llm.calls[0].system
    await say(session, "Perdona, dime.")
    assert session.state is CallState.HOOK
    roles = [m.role for m in llm.calls[1].messages]
    assert all(a != b for a, b in itertools.pairwise(roles))


async def test_short_script_uses_three_turns(script: CallScript) -> None:
    llm = FakeLLM(text_replies=["¿Me llevas?", "Gracias, adiós."])
    session = make_session(script.model_copy(update={"max_turns": 3}), llm)
    session.opening_line()
    await say(session, "¿Sí?")
    assert session.state is CallState.ASK
    await say(session, "Vale.")
    assert session.is_final_turn


def test_end_is_idempotent(script: CallScript) -> None:
    session = make_session(script, FakeLLM())
    session.opening_line()
    session.end("hangup")
    session.end("script-complete")
    assert session.state is CallState.ENDED
    assert session.end_reason == "hangup"
    assert session.snapshot().end_reason == "hangup"


# --- safe word: the call keeps going in the same voice -------------------------------------------

PHRASE = parse_safe_word("Pink flamingo")

SAFE_REPLIES = [
    "Es que me he dejado las llaves dentro.",
    "Bueno, que me quedo sin batería, ven ya.",
    "Un beso.",
]


def make_safe_session(script: CallScript, llm: FakeLLM, fired: list[str]) -> CallSession:
    async def on_safe_word() -> None:
        fired.append("alert")

    return CallSession(
        call_id="c1",
        script=script,
        llm=llm,
        safe_word=SafeWordMatcher(PHRASE),
        on_safe_word=on_safe_word,
    )


async def test_safe_word_fires_once_and_the_call_winds_down_in_the_same_voice(
    script: CallScript,
) -> None:
    llm = FakeLLM(text_replies=list(SAFE_REPLIES))
    fired: list[str] = []
    session = make_safe_session(script, llm, fired)
    session.opening_line()
    await say(session, "¿Sí?")
    assert session.state is CallState.HOOK
    reply = await say(session, "Ay, pink flamingo, ¿qué pasa?")
    await asyncio.sleep(0)
    assert fired == ["alert"]
    assert reply == SAFE_REPLIES[1]
    wrap_call = llm.calls[1]
    # Same persona prompt, only the turn instruction changes; the phrase never reaches the model.
    assert "Tía Carmen" in wrap_call.system
    assert "Keep exactly the same tone" in wrap_call.system
    assert all("flamingo" not in m.content for m in wrap_call.messages)
    assert wrap_call.messages[-1].content == "ay que pasa"
    await say(session, "pink flamingo otra vez")
    await asyncio.sleep(0)
    assert session.is_final_turn
    assert fired == ["alert"]


async def test_safe_word_during_the_goodbye_still_alerts(script: CallScript) -> None:
    llm = FakeLLM(text_replies=["a", "b", "c", "Un beso."])
    fired: list[str] = []
    session = make_safe_session(script, llm, fired)
    session.opening_line()
    for text in ["uno", "dos", "tres", "cuatro"]:
        await say(session, text)
    assert session.is_final_turn
    assert await say(session, "pink flamingo") == ""
    await session.wait_for_alerts()
    assert fired == ["alert"]


async def test_a_failing_alert_never_breaks_the_call(script: CallScript) -> None:
    async def broken() -> None:
        raise RuntimeError("telegram down")

    llm = FakeLLM(text_replies=list(SAFE_REPLIES))
    session = CallSession(
        call_id="c1", script=script, llm=llm, safe_word=SafeWordMatcher(PHRASE), on_safe_word=broken
    )
    session.opening_line()
    assert await say(session, "pink flamingo") == SAFE_REPLIES[0]
    await session.wait_for_alerts()
    assert session.safe_word_heard
