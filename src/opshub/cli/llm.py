"""``opshub llm ...`` subcommands (Phase 17-B, ADR-0031).

LLM backends are not connectors (they take API keys for inference,
not SaaS event sync). ADR-0031 §決定 (4) places their auth under a
per-noun group so the ``connector`` group surface stays clean.

Surface:

* ``opshub llm auth set anthropic`` — store Anthropic API key.
* ``opshub llm auth set openai`` — store OpenAI API key (distinct
  from the embedder slot per ADR-0015 §決定 (a) — extras
  independence).

There is intentionally no ``auth test`` for LLM backends —
verification happens at first ``brief`` / first ``propose generate``
invocation when the client makes its first inference call.
"""

from __future__ import annotations

import typer

llm_app = typer.Typer(
    name="llm",
    help="LLM backend (Anthropic / OpenAI) API key management.",
    no_args_is_help=True,
)

llm_auth_app = typer.Typer(
    name="auth",
    help="LLM backend API key management.",
    no_args_is_help=True,
)
llm_app.add_typer(llm_auth_app)


@llm_auth_app.command("set")
def llm_auth_set(
    vendor: str = typer.Argument(
        ...,
        help="LLM backend vendor: anthropic | openai.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="API key value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store an LLM backend API key in the OS keychain (ADR-0014).

    Supported vendors:

    * ``anthropic`` — stored under ``llm:anthropic:api_key`` (consumed
      by :class:`opshub.llm.anthropic_client.AnthropicLLMClient`).
    * ``openai`` — stored under ``llm:openai:api_key`` (consumed by
      :class:`opshub.llm.openai_client.OpenAILLMClient`). Distinct
      from the ``embedder:openai`` slot per ADR-0015 §決定 (a).

    Override at runtime without touching the keychain with the
    matching ``OPSHUB_LLM_<VENDOR>_API_KEY`` env var.
    """
    from opshub.cli._auth_common import set_token_credential

    if vendor == "anthropic":
        from opshub.llm.anthropic_client import ANTHROPIC_API_KEY_SECRET

        key = ANTHROPIC_API_KEY_SECRET
        label = "llm:anthropic"
    elif vendor == "openai":
        from opshub.llm.openai_client import OPENAI_API_KEY_SECRET as LLM_OPENAI_API_KEY_SECRET

        key = LLM_OPENAI_API_KEY_SECRET
        label = "llm:openai"
    else:
        typer.echo(
            f"unknown LLM vendor {vendor!r}; supported: anthropic, openai",
            err=True,
        )
        raise typer.Exit(code=2)

    set_token_credential(label=label, keyring_key=key, token=token)
