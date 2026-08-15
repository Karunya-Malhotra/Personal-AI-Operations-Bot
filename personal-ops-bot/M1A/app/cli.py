"""The `aiops` command line: the first interface, and in M1A the only one.

Why the CLI comes before Telegram (v0.3.1 roadmap, M1A vs M1E):

The interesting parts of this system are context assembly, tool selection and
policy. None of them care where a message came from. Building Telegram first
means debugging webhooks, tunnels, tokens, polling loops and media download
APIs *at the same time* as the parts that are actually hard. The CLI removes
that entire category of noise from the phase where it would cost the most.

Why there is no `MessageProvider` interface yet:

There is one implementation and no second consumer. Writing the interface now
would mean guessing at what Telegram and WhatsApp need rather than deriving it
from them, and a guessed interface is worse than none -- it looks authoritative
while encoding assumptions nobody checked. The interface arrives in M1B/M1E,
when there is a second implementation to derive it from. This is the §50 rule
applied to our own code.

In M1A `chat` echoes. In M1B the echo is replaced by the Agent Runtime, and
this file barely changes -- which is the point of keeping it this thin.
"""

from __future__ import annotations

import asyncio

import typer

from app.bootstrap import build_container
from app.config.settings import Settings

app = typer.Typer(add_completion=False, help="Personal AI operations bot.")

BANNER = """\
aiops {version} [{env}]
Type a message and press Enter. Ctrl-D or /quit to exit.
M1A: this is an echo. The agent runtime arrives in M1B.
"""


@app.command()
def chat(
    check_db: bool = typer.Option(
        True,
        "--check-db/--no-check-db",
        help="Run boot checks (including the dev/prod database guard) before starting.",
    ),
) -> None:
    """Start an interactive session."""
    settings = Settings()
    container = build_container(settings)

    if check_db:
        try:
            asyncio.run(container.startup())
        except Exception as exc:
            typer.secho(f"startup failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    print(BANNER.format(version="0.1.0", env=settings.app_env.value))

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            break
        print(handle_message(line))

    asyncio.run(container.shutdown())


def handle_message(text: str) -> str:
    """The seam the Agent Runtime replaces in M1B.

    Kept as a plain function rather than inlined into the loop so that M1B's
    first test can assert on the turn's output without driving a terminal.
    """
    return f"echo: {text}"


@app.command()
def env() -> None:
    """Print the effective (non-secret) configuration. Useful for diagnosing .env issues."""
    settings = Settings()
    print(f"app_env       : {settings.app_env.value}")
    print(f"database_host : {settings.database_host}")
    print(f"log_level     : {settings.log_level}")
    print(f"log_json      : {settings.use_json_logs}")
    print(f"api           : {settings.api_host}:{settings.api_port}")


if __name__ == "__main__":
    app()
