"""The `aiops` command line: the first interface, and in M1B the only one.

Why the CLI came before Telegram (v0.3.1 roadmap, M1A vs M1E):

The interesting parts of this system are context assembly, the run state machine
and -- from M1C -- policy. None of them care where a message came from. Building
Telegram first would mean debugging webhooks, tunnels, tokens and media APIs *at
the same time* as the parts that are actually hard. The CLI removes that whole
category of noise from the phase where it costs the most.

## The CLI holds no business logic (your §21)

Everything below is one of three things: reading a line, dispatching a slash
command, or printing a result. Deciding what a turn *does* belongs to the Agent
Runtime; deciding which conversation you are in belongs to the conversation
service. That split is what makes M1E cheap -- a Telegram adapter replaces this
file and nothing under it -- and it is why `handle_user_message` returns a
`TurnResult` rather than a formatted string. Formatting is the interface's job;
deciding is not.

## Why the loop is async and `input` runs in a thread

The runtime is async and holds a real connection pool. A blocking `input()` on
the event loop would stall asyncpg's background tasks for as long as you sat
looking at the prompt, which is how idle connections quietly die. One
`asyncio.run` for the whole session, with `input` moved to a thread, keeps the
pool healthy and the turn code identical to what a server would run.
"""

from __future__ import annotations

import asyncio

import typer

from app.agent.runtime import TurnResult
from app.bootstrap import Container, build_container
from app.config.settings import Settings
from app.domains.conversations import ConversationService

app = typer.Typer(add_completion=False, help="Personal AI operations bot.")

BANNER = """\
aiops {version} [{env}]  ·  {provider}/{model}
conversation {conversation} ({state})
Type a message and press Enter. /help for commands, /quit to exit.
"""

HELP = """\
  /new     start a fresh conversation
  /help    this list
  /quit    exit (Ctrl-D also works)
"""


@app.command()
def chat(
    check_db: bool = typer.Option(
        True,
        "--check-db/--no-check-db",
        help="Run boot checks (database guard, orphaned-run sweep) before starting.",
    ),
    new: bool = typer.Option(
        False, "--new", help="Start a new conversation instead of resuming the latest."
    ),
) -> None:
    """Start an interactive session."""
    settings = Settings()
    container = build_container(settings)
    raise SystemExit(asyncio.run(_session(container, check_db=check_db, start_new=new)))


async def _session(container: Container, *, check_db: bool, start_new: bool) -> int:
    if check_db:
        try:
            await container.startup()
        except Exception as exc:
            typer.secho(f"startup failed: {exc}", fg=typer.colors.RED, err=True)
            await container.shutdown()
            return 1

    try:
        async with container.session_factory() as session, session.begin():
            service = ConversationService(session, container.clock)
            user = await service.ensure_local_owner()
            if start_new:
                conversation, resumed = await service.start_conversation(user_id=user.id), False
            else:
                conversation, resumed = await service.resume_or_start(user_id=user.id)
            user_id, conversation_id = user.id, conversation.id

        print(
            BANNER.format(
                version="0.1.0",
                env=container.settings.app_env.value,
                provider=container.llm.name,
                model=container.settings.llm_model,
                conversation=str(conversation_id)[:8],
                state="resumed" if resumed else "new",
            )
        )

        while True:
            try:
                line = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            if line == "/help":
                print(HELP)
                continue
            if line == "/new":
                async with container.session_factory() as session, session.begin():
                    conversation = await ConversationService(
                        session, container.clock
                    ).start_conversation(user_id=user_id)
                    conversation_id = conversation.id
                print(f"started conversation {str(conversation_id)[:8]}")
                continue

            result = await container.runtime.handle_user_message(
                conversation_id=conversation_id, user_id=user_id, content=line
            )
            print(render(result))

        return 0
    finally:
        await container.shutdown()


def render(result: TurnResult) -> str:
    """Turn a `TurnResult` into what the owner sees.

    A failed run is reported as a failure, in the owner's words, naming what
    went wrong. §15A and §26 Scenario 4: an outage must never be dressed up as
    the assistant having nothing to say -- silence would read as an answer.
    """
    if result.ok and result.reply is not None:
        return result.reply

    reason = {
        "LLMTimeout": "the model did not respond in time",
        "LLMUnavailable": "the model provider is unreachable",
        "LLMRateLimited": "the model provider is rate limiting us",
        "LLMAuthError": "the provider rejected our credentials",
        "LLMMalformedResponse": "the provider returned a response we could not read",
        "LLMInvalidRequest": "we sent a request the provider rejected",
        "context_error": "the conversation history could not be assembled",
    }.get(result.failure_kind or "", "an unexpected error occurred")

    return (
        f"[no answer: {reason}]\n"
        f"  nothing was saved for this turn beyond your message.\n"
        f"  run {result.run_id} ({result.state.value}) has the details."
    )


@app.command()
def env() -> None:
    """Print the effective (non-secret) configuration. Useful for diagnosing .env issues."""
    settings = Settings()
    print(f"app_env       : {settings.app_env.value}")
    print(f"database_host : {settings.database_host}")
    print(f"llm_provider  : {settings.llm_provider}")
    print(f"llm_model     : {settings.llm_model}")
    print(f"context_window: {settings.context_window_messages} messages")
    print(f"log_level     : {settings.log_level}")
    print(f"log_json      : {settings.use_json_logs}")
    print(f"api           : {settings.api_host}:{settings.api_port}")


if __name__ == "__main__":
    app()
