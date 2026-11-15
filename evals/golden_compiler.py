"""Golden cases for the excuse compiler, run against the real strong model.

Each case in evals/golden/excuses.yaml states the message and checkable expectations (refusal kind,
language, delay, urgency floor, caller gender, register). Free-text fields are not compared; an
accepted script must also pass ScriptLinter (the compiler enforces that). Prints a pass table and
the total cost.

    uv run poe eval-golden
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from imbusy.call.voices import VoiceCatalog
from imbusy.config import get_settings
from imbusy.excuse.compiler import ExcuseCompiler
from imbusy.excuse.schema import CompiledExcuse, ExcuseRequest
from imbusy.llm import LLMClient, build_llm

GOLDEN_PATH = Path(__file__).parent / "golden" / "excuses.yaml"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]


def check(expect: dict[str, Any], result: object, gender_of_voice: str | None) -> list[str]:
    """Compare a compile result with a case's expectations; returns failure descriptions."""
    failures: list[str] = []
    refusal = expect.get("refusal", "missing")
    if refusal not in (None, "missing"):
        kind = getattr(result, "kind", None)
        if kind is None or kind.value != refusal:
            failures.append(f"expected refusal {refusal}, got {kind or 'a script'}")
        return failures
    if not isinstance(result, CompiledExcuse):
        return [f"expected a script, got refusal {getattr(result, 'kind', '?')}"]
    script = result.script
    if "language" in expect and script.language.value != expect["language"]:
        failures.append(f"language {script.language.value} != {expect['language']}")
    expected_delay = expect.get("delay_seconds")
    if expected_delay is not None and abs(result.delay_seconds - int(expected_delay)) > 30:
        failures.append(f"delay {result.delay_seconds} != {expect['delay_seconds']}")
    if "speech_register" in expect and script.speech_register.value != expect["speech_register"]:
        failures.append(f"register {script.speech_register.value} != {expect['speech_register']}")
    if "urgency_min" in expect and script.urgency < int(expect["urgency_min"]):
        failures.append(f"urgency {script.urgency} < {expect['urgency_min']}")
    if "caller_gender" in expect and gender_of_voice != expect["caller_gender"]:
        failures.append(f"voice gender {gender_of_voice} != {expect['caller_gender']}")
    return failures


async def run(llm: LLMClient, path: Path = GOLDEN_PATH) -> list[CaseResult]:
    settings = get_settings()
    voices = VoiceCatalog(settings.twilio_tts_provider)
    # No default zone: each case's own UTC offset stands in for the user's local time.
    compiler = ExcuseCompiler(
        llm,
        voices,
        default_delay_seconds=20,
        max_delay_seconds=settings.max_call_delay_seconds,
        default_timezone="",
    )
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    results: list[CaseResult] = []
    for case in cases:
        now = datetime.fromisoformat(case["now"])
        request = ExcuseRequest(text=case["text"], source="demo", received_at=now)
        result = await compiler.compile(request)
        gender = (
            voices.get(result.script.voice_id).gender.value
            if isinstance(result, CompiledExcuse)
            else None
        )
        failures = check(case.get("expect") or {}, result, gender)
        results.append(CaseResult(case["id"], not failures, tuple(failures)))
    return results


def main() -> None:
    settings = get_settings()
    if not settings.llm_enabled:
        sys.exit("ANTHROPIC_API_KEY is not set; the golden eval needs the real model")
    results = asyncio.run(run(build_llm(settings)))
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.case_id:<36} {'; '.join(r.failures)}")
    print(
        f"{sum(r.passed for r in results)} / {len(results)} passed (costs are in the llm_call log lines)"
    )


if __name__ == "__main__":
    main()
