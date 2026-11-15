"""Turn latency eval: p50/p95 from final user transcript to first caller token sent.

Drives the configured fast model through the same RelayHandler the phone path uses, over an
in-memory transport, with a fixed bank of user lines per language and the fixture script for that
language. Writes evals/results/latency-<timestamp>.json.

    uv run poe eval                                  # 20 calls x 4 turns per language, real model
    uv run python -m evals.latency --calls 5 --language es-ES
    uv run python -m evals.latency --fixture         # no key: checks the harness, numbers are fake

What it does not measure: Twilio's end-of-speech detection and TTS start on a real call, and the
browser's speech recognition and synthesis in the demo. The demo page reports CLIENT_FIRST_AUDIO
for that; real-call numbers come from the `call finished` log lines of real calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from imbusy.call.latency import LatencyKind, LatencyRecorder, percentile
from imbusy.call.relay import PromptMessage, QueueTransport, RelayHandler, SetupMessage
from imbusy.call.session import CallSession
from imbusy.call.voices import VoiceCatalog
from imbusy.config import TtsProvider, get_settings
from imbusy.excuse.compiler import ExcuseCompiler
from imbusy.excuse.schema import CompiledExcuse, ExcuseDraft, ExcuseRequest, Language
from imbusy.fixtures import fixture_call
from imbusy.llm import FakeLLM, LLMClient, build_llm

USER_LINES: dict[Language, list[str]] = {
    Language.ES_ES: ["¿Sí? Dime.", "¿Qué ha pasado?", "Vale, vale, ¿y ahora qué hago?", "Vale, voy para allá."],
    Language.EN_US: ["Hey, what's up?", "Wait, what happened?", "Okay, what do you need?", "Alright, I'm on my way."],
}  # fmt: skip


@dataclass(frozen=True)
class LatencyReport:
    language: Language
    samples: int
    p50_ms: float
    p95_ms: float
    model: str
    prompt_version: int


def fixture_script(language: Language) -> CompiledExcuse:
    draft = ExcuseDraft.model_validate(fixture_call(language)["draft"])
    compiler = ExcuseCompiler(
        FakeLLM(), VoiceCatalog(TtsProvider.GOOGLE), default_delay_seconds=0, max_delay_seconds=0
    )
    request = ExcuseRequest(text="eval", source="demo", received_at=datetime.now(UTC))
    result = compiler.finalise(draft, request)
    assert isinstance(result, CompiledExcuse)
    return result


async def one_call(llm: LLMClient, language: Language) -> list[float]:
    excuse = fixture_script(language)
    transport = QueueTransport()
    recorded: list[LatencyRecorder] = []

    async def resolve(setup: SetupMessage) -> CallSession | None:
        return CallSession(call_id=str(uuid4()), script=excuse.script, llm=llm)

    async def on_finish(session: CallSession, latency: LatencyRecorder) -> None:
        recorded.append(latency)

    handler = RelayHandler(
        transport, resolve, send_greeting=False, silence_timeout=None, playback_scale=0.0,
        on_finish=on_finish,
    )  # fmt: skip
    transport.push(SetupMessage(type="setup", session_id="eval", call_sid="eval"))
    task = asyncio.create_task(handler.run())
    for line in USER_LINES[language]:
        transport.push(PromptMessage(type="prompt", voice_prompt=line, lang=language.value))
        await transport.next_reply(within=60)
    await asyncio.wait_for(task, 10)
    return [s.milliseconds for s in recorded[0].samples if s.kind is LatencyKind.SERVER_FIRST_TOKEN]


async def run(
    llm: LLMClient, model: str, calls: int, languages: list[Language], out_dir: Path
) -> list[LatencyReport]:
    """Run `calls` simulated calls per language and return one report per language."""
    reports: list[LatencyReport] = []
    for language in languages:
        samples: list[float] = []
        for _ in range(calls):
            samples.extend(await one_call(llm, language))
        reports.append(
            LatencyReport(
                language=language,
                samples=len(samples),
                p50_ms=round(percentile(samples, 50), 1),
                p95_ms=round(percentile(samples, 95), 1),
                model=model,
                prompt_version=fixture_script(language).prompt_version,
            )
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"latency-{stamp}.json"
    path.write_text(json.dumps([asdict(r) for r in reports], indent=2), encoding="utf-8")
    print(f"wrote {path}")
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn latency p50/p95 (server side).")
    parser.add_argument("--calls", type=int, default=20)
    parser.add_argument("--language", choices=[lang.value for lang in Language], action="append")
    parser.add_argument("--out", type=Path, default=Path("evals/results"))
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="fake model with 120 ms first token; checks the harness",
    )
    args = parser.parse_args()
    languages = [Language(v) for v in args.language] if args.language else list(Language)
    if args.fixture:
        replies = [
            line
            for lang in languages
            for _ in range(args.calls)
            for line in fixture_call(lang)["lines"].values()
        ]
        llm: LLMClient = FakeLLM(
            text_replies=replies * 2, first_token_delay_seconds=0.12, token_delay_seconds=0.01
        )
        model = "fixture (fake 120 ms first token)"
    else:
        settings = get_settings()
        if not settings.llm_enabled:
            sys.exit(
                "ANTHROPIC_API_KEY is not set; use --fixture to check the harness without a key"
            )
        llm, model = build_llm(settings), settings.llm_fast_model
    reports = asyncio.run(run(llm, model, args.calls, languages, args.out))
    print(f"{'language':<8} {'n':>4} {'p50 ms':>8} {'p95 ms':>8}  model")
    for r in reports:
        print(f"{r.language.value:<8} {r.samples:>4} {r.p50_ms:>8} {r.p95_ms:>8}  {r.model}")


if __name__ == "__main__":
    main()
