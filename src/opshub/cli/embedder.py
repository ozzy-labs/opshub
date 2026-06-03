"""``opshub embedder ...`` subcommands (Phase 17-B, ADR-0031).

Embedders are not connectors (they take API keys for inference, not
SaaS event sync). ADR-0031 §決定 (4) places their auth under a
per-noun group so the ``connector`` group surface stays clean.

Surface:

* ``opshub embedder auth set openai`` — store OpenAI API key.
* ``opshub embedder auth set voyage`` — store Voyage API key.

There is intentionally no ``auth test`` for embedders — verification
happens at first ``brief`` / first ``recall`` invocation when the
embedder makes its first inference call.
"""

from __future__ import annotations

import typer

embedder_app = typer.Typer(
    name="embedder",
    help="Embedder backend (OpenAI / Voyage) API key management.",
    no_args_is_help=True,
)

embedder_auth_app = typer.Typer(
    name="auth",
    help="Embedder backend API key management.",
    no_args_is_help=True,
)
embedder_app.add_typer(embedder_auth_app)


@embedder_auth_app.command("set")
def embedder_auth_set(
    vendor: str = typer.Argument(
        ...,
        help="Embedder backend vendor: openai | voyage.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="API key value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store an embedder backend API key in the OS keychain (ADR-0014).

    Supported vendors:

    * ``openai`` — stored under ``embedder:openai:api_key``
      (consumed by :class:`opshub.vectors.openai_embedder.OpenAIEmbedder`).
    * ``voyage`` — stored under ``embedder:voyage:api_key``
      (consumed by :class:`opshub.vectors.voyage_embedder.VoyageEmbedder`).

    Override at runtime without touching the keychain with the
    matching ``OPSHUB_EMBEDDER_<VENDOR>_API_KEY`` env var.
    """
    from opshub.cli._auth_common import set_token_credential

    if vendor == "openai":
        from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET

        key = OPENAI_API_KEY_SECRET
        label = "embedder:openai"
    elif vendor == "voyage":
        from opshub.vectors.voyage_embedder import VOYAGE_API_KEY_SECRET

        key = VOYAGE_API_KEY_SECRET
        label = "embedder:voyage"
    else:
        typer.echo(
            f"unknown embedder vendor {vendor!r}; supported: openai, voyage",
            err=True,
        )
        raise typer.Exit(code=2)

    set_token_credential(label=label, keyring_key=key, token=token)
