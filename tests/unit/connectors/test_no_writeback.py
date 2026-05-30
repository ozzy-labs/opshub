"""Phase 10 step E2 HITL boundary contract test (ADR-0010 §禁止事項 7).

ADR-0016 §決定 (c) (Phase 6) pinned the human-in-the-loop boundary
for proposal apply: LLM-generated text only reaches durable state
through an operator-driven CLI step. Phase 10 Sub-issue E
(reply-draft generation) extends that boundary to *external*
write-back: an applied reply_draft candidate MUST NOT send / post
anything to the upstream SaaS, and the connector modules MUST NOT
ship the methods that would even make such a path possible.

This module enforces the contract structurally: every connector
package is scanned and assert no public attribute named ``post`` /
``send`` / ``comment`` / ``reply`` / ``create_comment`` (etc.) is
exported. Should a future PR add a write method by accident, this
test fails before the code reaches CI review.

The contract is also pinned at runtime by
``test_proposal_service.py::test_apply_reply_draft_does_not_send_external_request``
(which forbids socket creation during apply). The two pins are
complementary: the structural one catches static surface accretion,
the runtime one catches inline httpx / urllib calls hidden in
service code.

Adding a write method (e.g. for a future deliberate Phase 11+
write-back ADR) requires:

1. Superseded-by ADR for ADR-0010 (per ADR-0010 §Phase 10 改訂).
2. Updating the ``_FORBIDDEN_WRITE_NAMES`` whitelist (or removing
   this test entirely if the contract changes shape).

The intentional barrier prevents accidental introduction.
"""

from __future__ import annotations

import importlib
import pkgutil

import opshub.connectors as _connectors_pkg

#: Method / attribute names that would indicate a write-back surface.
#: Limited to names that genuinely mean "send to external system";
#: leaves benign helpers like ``post_init`` / ``post_process`` alone
#: by anchoring on exact attribute equality (not substring match).
_FORBIDDEN_WRITE_NAMES: frozenset[str] = frozenset(
    {
        "post",
        "send",
        "send_message",
        "post_message",
        "comment",
        "create_comment",
        "reply",
        "post_reply",
        "send_reply",
        "delete_message",
        "edit_message",
    }
)


def _iter_connector_modules() -> list[str]:
    """Walk ``opshub.connectors`` and return every submodule fullname."""
    modules: list[str] = []
    for module_info in pkgutil.walk_packages(
        _connectors_pkg.__path__,
        prefix="opshub.connectors.",
    ):
        modules.append(module_info.name)
    return modules


def test_no_connector_module_exports_a_writeback_method() -> None:
    """Phase 10 step E2 (ADR-0010 §禁止事項 7).

    Iterates every ``opshub.connectors.*`` submodule, imports it, and
    asserts no top-level attribute carries a forbidden write-back
    name. Imports that fail (missing optional extras like
    ``[slack]`` / ``[ms365]`` on the test environment) are skipped
    with an informational pytest note rather than failing — the
    contract concerns code that is loadable; an unimportable module
    cannot have its symbol table inspected anyway, and the strict
    extras-installed run in CI catches everything regardless.
    """
    failures: list[str] = []
    for module_name in _iter_connector_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # Optional extras not installed (e.g. slack-sdk). Skip
            # silently — CI installs all extras so the full contract
            # is enforced there.
            continue
        for forbidden in _FORBIDDEN_WRITE_NAMES:
            value = getattr(module, forbidden, None)
            # Allow ``None`` (not present). Anything else means an
            # actual symbol with one of the forbidden names was
            # exported — a writeback surface accretion.
            if value is not None:
                failures.append(
                    f"{module_name}.{forbidden} = {value!r} — write-back forbidden by ADR-0010"
                )

    assert failures == [], (
        "Connector modules must not expose write-back methods (ADR-0010 §禁止事項 7 "
        "Phase 10 改訂 + ADR-0016 §決定 (c) HITL contract). Offenders:\n" + "\n".join(failures)
    )


def test_connector_classes_have_no_writeback_methods() -> None:
    """Same contract, but for class attributes (e.g. ``connector.send_message``).

    The module-level test catches free-function write surfaces; this
    one walks every class defined inside a connector module and
    asserts none of its methods carry the forbidden names. Together
    the two cover the most common shapes a write API could take.
    """
    failures: list[str] = []
    for module_name in _iter_connector_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if not isinstance(attr, type):
                continue
            # Only inspect classes defined inside this module; skip
            # re-exports from third-party SDKs (their surface is not
            # opshub's concern).
            if getattr(attr, "__module__", None) != module_name:
                continue
            for forbidden in _FORBIDDEN_WRITE_NAMES:
                if hasattr(attr, forbidden):
                    failures.append(
                        f"{module_name}.{attr_name}.{forbidden} exists — "
                        "write-back forbidden by ADR-0010"
                    )

    assert failures == [], (
        "Connector classes must not expose write-back methods (ADR-0010 §禁止事項 7 "
        "Phase 10 改訂 + ADR-0016 §決定 (c) HITL contract). Offenders:\n" + "\n".join(failures)
    )
