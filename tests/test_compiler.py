"""ExcuseCompiler: deterministic finalisation and golden cases with a fake model."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from imbusy.call.voices import VoiceCatalog
from imbusy.config import TtsProvider
from imbusy.excuse.compiler import (
    REFUSAL_MESSAGES,
    ExcuseCompiler,
    clamp_delay,
    forbidden_rules,
    guess_language,
    trim_opening,
)
from imbusy.excuse.schema import (
    BASE_FORBIDDEN,
    CompiledExcuse,
    CompileRefused,
    ExcuseDraft,
    ExcuseRequest,
    Language,
    RefusalKind,
    VoiceGender,
)
from imbusy.llm import FakeLLM, ModelRole
from imbusy.prompts import load_prompt
from imbusy.safety.guard import ScriptLinter

GOLDEN = Path(__file__).parents[1] / "evals" / "golden" / "excuses.yaml"


def make_compiler(llm: FakeLLM, provider: TtsProvider = TtsProvider.GOOGLE) -> ExcuseCompiler:
    return ExcuseCompiler(
        llm, VoiceCatalog(provider), default_delay_seconds=20, max_delay_seconds=900
    )


def test_clamp_delay() -> None:
    assert clamp_delay(None, default=20, maximum=900) == 20
    assert clamp_delay(-5, default=20, maximum=900) == 0
    assert clamp_delay(5000, default=20, maximum=900) == 900
    assert clamp_delay(120, default=20, maximum=900) == 120


def test_forbidden_rules_keep_base_rules_first_and_dedupe() -> None:
    rules = forbidden_rules([BASE_FORBIDDEN[0], "Never mention the user's partner."])
    assert rules[: len(BASE_FORBIDDEN)] == list(BASE_FORBIDDEN)
    assert rules.count(BASE_FORBIDDEN[0]) == 1
    assert rules[-1] == "Never mention the user's partner."


def test_compiler_prompt_has_version_header_and_time_placeholder() -> None:
    prompt = load_prompt("imbusy.excuse", "compiler.md")
    assert prompt.id == "excuse-compiler"
    assert prompt.version >= 1
    assert "$now" in prompt.body


def test_finalise_picks_catalog_voice_for_language_and_gender(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    """A female es-ES draft gets an es-ES female voice from the configured provider."""
    for provider, expected in [
        (TtsProvider.GOOGLE, "es-f-aoede"),
        (TtsProvider.AMAZON, "es-f-lucia"),
    ]:
        result = make_compiler(FakeLLM(), provider).finalise(draft, excuse_request)
        assert isinstance(result, CompiledExcuse)
        assert result.script.voice_id == expected
    male = draft.model_copy(update={"caller_gender": VoiceGender.MALE})
    result = make_compiler(FakeLLM()).finalise(male, excuse_request)
    assert isinstance(result, CompiledExcuse)
    assert result.script.voice_id == "es-m-charon"


def test_finalise_clamps_urgency_into_range(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    """The fixture draft says urgency 7; the script must carry 5."""
    result = make_compiler(FakeLLM()).finalise(draft, excuse_request)
    assert isinstance(result, CompiledExcuse)
    assert result.script.urgency == 5
    low = make_compiler(FakeLLM()).finalise(
        draft.model_copy(update={"urgency": -3}), excuse_request
    )
    assert isinstance(low, CompiledExcuse)
    assert low.script.urgency == 1


def test_finalise_delay(draft: ExcuseDraft, excuse_request: ExcuseRequest) -> None:
    compiler = make_compiler(FakeLLM())
    result = compiler.finalise(draft, excuse_request)
    assert isinstance(result, CompiledExcuse) and result.delay_seconds == 120
    unset = compiler.finalise(draft.model_copy(update={"delay_seconds": None}), excuse_request)
    assert isinstance(unset, CompiledExcuse) and unset.delay_seconds == 20
    huge = compiler.finalise(draft.model_copy(update={"delay_seconds": 86_400}), excuse_request)
    assert isinstance(huge, CompiledExcuse) and huge.delay_seconds == 900


def test_finalise_appends_base_forbidden_rules(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    result = make_compiler(FakeLLM()).finalise(draft, excuse_request)
    assert isinstance(result, CompiledExcuse)
    assert list(result.script.forbidden[: len(BASE_FORBIDDEN)]) == list(BASE_FORBIDDEN)


def test_trim_opening_keeps_whole_sentences_under_limit() -> None:
    short = "¿Hola? Oye, soy Carmen. ¿Tienes un minuto?"
    assert trim_opening(short) == short
    long = (
        "¿Mari? Soy Carmen. "
        "Te llamo porque he bajado a tirar la basura y resulta que se me ha cerrado la puerta "
        "con las llaves dentro y el cerrajero no puede venir hasta las nueve."
    )
    assert trim_opening(long) == "¿Mari? Soy Carmen."
    run_on = " ".join(["palabra"] * 40)
    assert len(trim_opening(run_on).split()) == 25


def test_finalise_trims_opening_line(draft: ExcuseDraft, excuse_request: ExcuseRequest) -> None:
    long_opening = "Hola, soy Carmen. " + " ".join(["bla"] * 30) + "."
    result = make_compiler(FakeLLM()).finalise(
        draft.model_copy(update={"opening_line": long_opening}), excuse_request
    )
    assert isinstance(result, CompiledExcuse)
    assert result.script.opening_line == "Hola, soy Carmen."


def test_refusal_draft_becomes_compile_refused_with_localised_message(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    refusal = draft.model_copy(update={"refusal": RefusalKind.IMPERSONATES_AUTHORITY})
    result = make_compiler(FakeLLM()).finalise(refusal, excuse_request)
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.IMPERSONATES_AUTHORITY
    assert result.message == REFUSAL_MESSAGES[Language.ES_ES][RefusalKind.IMPERSONATES_AUTHORITY]


def test_model_playing_an_authority_is_still_refused(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    """Second line of defence: the model ignored the prompt and played the police."""
    bad = draft.model_copy(update={"caller_name": "Policía Nacional"})
    result = make_compiler(FakeLLM()).finalise(bad, excuse_request)
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.IMPERSONATES_AUTHORITY


def test_lint_failure_is_a_refusal(draft: ExcuseDraft, excuse_request: ExcuseRequest) -> None:
    """The caller saying it is an AI is caught by the script linter."""
    bad = draft.model_copy(update={"situation": "Soy un robot que te llama."})
    result = make_compiler(FakeLLM()).finalise(bad, excuse_request)
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.HARMFUL


def test_empty_fields_are_not_an_excuse(draft: ExcuseDraft, excuse_request: ExcuseRequest) -> None:
    result = make_compiler(FakeLLM()).finalise(
        draft.model_copy(update={"situation": "   "}), excuse_request
    )
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.NOT_AN_EXCUSE


async def test_api_refusal_maps_to_harmful(excuse_request: ExcuseRequest) -> None:
    """A StopReason.REFUSAL from the model yields CompileRefused(kind=HARMFUL), never an exception."""
    result = await make_compiler(FakeLLM(structured_responses=[None])).compile(excuse_request)
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.HARMFUL
    assert result.message == REFUSAL_MESSAGES[Language.ES_ES][RefusalKind.HARMFUL]


async def test_compile_uses_strong_model_and_local_time(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    llm = FakeLLM(structured_responses=[draft])
    result = await make_compiler(llm).compile(excuse_request)
    assert isinstance(result, CompiledExcuse)
    call = llm.calls[0]
    assert call.role is ModelRole.STRONG
    assert call.output_type is ExcuseDraft
    # 17:45 UTC is 18:45 in Madrid in November.
    assert "18:45" in call.system
    assert call.messages[0].content == excuse_request.text


async def test_preferred_language_is_passed_and_wins(
    draft: ExcuseDraft, excuse_request: ExcuseRequest
) -> None:
    llm = FakeLLM(structured_responses=[draft])
    request = excuse_request.model_copy(update={"preferred_language": Language.EN_US})
    result = await make_compiler(llm).compile(request)
    assert "(Call language: en-US)" in llm.calls[0].messages[0].content
    assert isinstance(result, CompiledExcuse)
    assert result.script.language is Language.EN_US


def test_system_prompt_renders_current_time() -> None:
    compiler = make_compiler(FakeLLM())
    now = datetime(2026, 11, 13, 18, 20, tzinfo=UTC)
    assert "Friday 13 November 2026, 18:20" in compiler.system_prompt(now)
    assert "$now" not in compiler.system_prompt(now)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("tía Carmen, llaves, en 2 min", Language.ES_ES),
        ("mamá, sin llaves", Language.ES_ES),
        ("my boss, the client call moved", Language.EN_US),
    ],
)
def test_guess_language(text: str, language: Language) -> None:
    assert guess_language(text) is language


def _golden_cases() -> list[dict[str, object]]:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    cases: list[dict[str, object]] = data["cases"]
    return cases


@pytest.mark.parametrize("case", _golden_cases(), ids=lambda c: str(c["id"]))
def test_golden_cases_parse_and_finalise(case: dict[str, object], draft: ExcuseDraft) -> None:
    """Each golden case, answered with a draft that meets its expectations, finalises cleanly:
    refusals stay refusals, accepted scripts pass the linter and keep the expected fields."""
    expect = dict(case.get("expect") or {})  # type: ignore[call-overload]
    updates: dict[str, object] = {}
    for key in ("refusal", "language", "delay_seconds", "caller_gender", "speech_register"):
        if key in expect:
            updates[key] = expect[key]
    if "urgency_min" in expect:
        updates["urgency"] = expect["urgency_min"]
    answer = ExcuseDraft.model_validate({**draft.model_dump(), **updates})
    request = ExcuseRequest(
        text=str(case["text"]), source="telegram", received_at=datetime.now(UTC)
    )
    result = make_compiler(FakeLLM()).finalise(answer, request)
    if expect.get("refusal"):
        assert isinstance(result, CompileRefused)
        assert result.kind.value == expect["refusal"]
    else:
        assert isinstance(result, CompiledExcuse)
        assert ScriptLinter().lint(result.script) == []
        if "delay_seconds" in expect:
            expected = 20 if expect["delay_seconds"] is None else expect["delay_seconds"]
            assert result.delay_seconds == expected


async def test_screened_requests_never_reach_the_model(excuse_request: ExcuseRequest) -> None:
    llm = FakeLLM()
    compiler = ExcuseCompiler(
        llm, VoiceCatalog(TtsProvider.GOOGLE), default_delay_seconds=20, max_delay_seconds=900
    )
    request = excuse_request.model_copy(update={"text": "que me llame Carmen López, mi jefa"})
    result = await compiler.compile(request)
    assert isinstance(result, CompileRefused)
    assert result.kind is RefusalKind.IMPERSONATES_REAL_PERSON
    assert "nombre de pila" in result.message
    assert llm.calls == []
