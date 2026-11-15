"""Command line: `imbusy compile`, `imbusy talk`, `imbusy doctor`, `imbusy telegram ...`.

Without ANTHROPIC_API_KEY, `compile` and `talk` run against the scripted fixture model.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import typer

from imbusy.call.session import CallSession
from imbusy.call.voices import VoiceCatalog
from imbusy.config import Settings, get_settings
from imbusy.excuse.compiler import ExcuseCompiler
from imbusy.excuse.schema import CompiledExcuse, ExcuseRequest, Language
from imbusy.fixtures import ScriptedLLM
from imbusy.llm import LLMClient, build_llm
from imbusy.telegram.client import TelegramClient, TelegramError

app = typer.Typer(no_args_is_help=True, add_completion=False)
telegram_app = typer.Typer(no_args_is_help=True, help="Manage the Telegram webhook.")
app.add_typer(telegram_app, name="telegram")


def _llm(settings: Settings) -> tuple[LLMClient, bool]:
    if settings.llm_enabled:
        return build_llm(settings), False
    return ScriptedLLM(token_delay_seconds=0.0), True


def _compiler(settings: Settings, llm: LLMClient) -> ExcuseCompiler:
    return ExcuseCompiler(
        llm,
        VoiceCatalog(settings.twilio_tts_provider),
        default_delay_seconds=settings.default_call_delay_seconds,
        max_delay_seconds=settings.max_call_delay_seconds,
        default_timezone=settings.default_timezone,
    )


def _request(text: str, language: str | None) -> ExcuseRequest:
    return ExcuseRequest(
        text=text,
        source="demo",
        preferred_language=Language(language) if language else None,
        received_at=datetime.now(UTC),
    )


@app.command("compile")
def compile_excuse(text: str, language: str | None = None) -> None:
    """Compile an excuse and print the CallScript JSON."""
    settings = get_settings()
    llm, fixture = _llm(settings)
    if fixture:
        typer.echo("(fixture mode: ANTHROPIC_API_KEY is not set)", err=True)
    result = asyncio.run(_compiler(settings, llm).compile(_request(text, language)))
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def talk(text: str, language: str | None = None) -> None:
    """Compile an excuse and play the call in the terminal: you type, the caller answers."""
    asyncio.run(_talk(text, language))


async def _talk(text: str, language: str | None) -> None:
    settings = get_settings()
    llm, fixture = _llm(settings)
    if fixture:
        typer.echo("(fixture mode: the caller follows a script and ignores what you type)")
    result = await _compiler(settings, llm).compile(_request(text, language))
    if not isinstance(result, CompiledExcuse):
        typer.echo(result.message)
        raise typer.Exit(1)
    session = CallSession(call_id=str(result.call_id), script=result.script, llm=llm)
    name = result.script.caller_name
    typer.echo(f"{name}: {session.opening_line()}")
    while session.accepts_input:
        try:
            line = await asyncio.to_thread(input, "you: ")
        except EOFError:
            break
        sys.stdout.write(f"{name}: ")
        async for chunk in session.respond(line):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        sys.stdout.write("\n")
    session.end("script-complete")


@app.command()
def doctor() -> None:
    """Report which modes are configured and what is missing, without printing secrets."""
    s = get_settings()
    checks = [
        ("ANTHROPIC_API_KEY", s.llm_enabled, "live model (otherwise fixture mode)"),
        ("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER", s.twilio_enabled, "real calls"),
        ("TELEGRAM_BOT_TOKEN / TELEGRAM_WEBHOOK_SECRET", s.telegram_enabled, "real calls"),
        ("PUBLIC_BASE_URL", s.public_base_url is not None, "real calls"),
        ("REDIS_URL", s.redis_url is not None, "shared script store (otherwise in-process)"),
    ]  # fmt: skip
    for name, ok, purpose in checks:
        typer.echo(f"[{'ok' if ok else '--'}] {name}: {purpose}")
    typer.echo(f"demo: {'on' if s.demo_enabled else 'off'}; real calls: "
               f"{'on' if s.real_calls_enabled else 'off'}")  # fmt: skip


def _telegram(settings: Settings) -> TelegramClient:
    if settings.telegram_bot_token is None:
        typer.echo("TELEGRAM_BOT_TOKEN is not set", err=True)
        raise typer.Exit(2)
    return TelegramClient(settings.telegram_bot_token.get_secret_value())


@telegram_app.command("set-webhook")
def set_webhook() -> None:
    """setWebhook to PUBLIC_BASE_URL/telegram/webhook with TELEGRAM_WEBHOOK_SECRET."""
    settings = get_settings()
    if settings.public_base_url is None or settings.telegram_webhook_secret is None:
        typer.echo("PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET are required", err=True)
        raise typer.Exit(2)
    url = settings.public_base_url + "/telegram/webhook"
    secret = settings.telegram_webhook_secret.get_secret_value()

    async def run() -> None:
        client = _telegram(settings)
        try:
            await client.set_webhook(url, secret)
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    except TelegramError as exc:
        typer.echo(f"Telegram refused: {exc.description}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Webhook set to {url}")


@telegram_app.command("delete-webhook")
def delete_webhook() -> None:
    settings = get_settings()

    async def run() -> None:
        client = _telegram(settings)
        try:
            await client.delete_webhook()
        finally:
            await client.aclose()

    asyncio.run(run())
    typer.echo("Webhook deleted")


if __name__ == "__main__":
    app()
