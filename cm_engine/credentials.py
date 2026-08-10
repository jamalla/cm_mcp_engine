"""On whose behalf a call is made, and which credential answers for them.

A seam, not an `os.environ` lookup at the call site, because the production shape
differs from this POC in two ways that are hard to retrofit later:

* **One credential per install, not per process.** Salla issues an access token
  per store that installs the app, with a refresh token and an expiry -- so the
  real resolver takes a principal and may refresh before returning.
* **Storage is a secrets manager, not an environment.** Rotation, audit, and
  least-privilege access to the secret itself all live there.

Both changes land behind `CredentialProvider.resolve`, and nothing above it moves.

The principal matters for a second reason that has nothing to do with storage: it
scopes the result cache. Two stores calling `list_categories` with identical
arguments must never share a cached answer, and the only thing standing between
them is that the principal is part of the cache key.

**Where the principal comes from is a security decision, not a convenience.** It
is derived from the authenticated session -- never from a tool argument. An agent
that could name the store it wants to read is a confused deputy: the model, or a
prompt that reached the model, would choose whose data comes back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cm_engine.config import Upstream, resolve_token

# The POC's single tenant. A deployment maps an authenticated MCP session to the
# merchant install behind it; there is no session to map here, so one is named
# explicitly rather than left implicit.
DEFAULT_PRINCIPAL_ID = os.environ.get("CM_PRINCIPAL", "local-dev")


@dataclass(frozen=True)
class Principal:
    """The caller a credential and a cached result belong to."""

    id: str
    label: str = ""

    @property
    def cache_scope(self) -> str:
        """What makes this principal's cache entries its own."""
        return self.id

    def __str__(self) -> str:
        return self.label or self.id


def default_principal() -> Principal:
    return Principal(id=DEFAULT_PRINCIPAL_ID, label="local development store")


class CredentialProvider:
    """Resolves the credential for one upstream, on behalf of one principal."""

    def resolve(self, upstream: Upstream, principal: Principal) -> str:  # pragma: no cover
        raise NotImplementedError


class EnvCredentials(CredentialProvider):
    """Reads one token per upstream from the engine's own environment.

    Correct for a single-tenant POC and for local development, and honest about
    being no more than that: it ignores the principal, because there is only one.
    A multi-tenant deployment replaces this class, not its callers.
    """

    def resolve(self, upstream: Upstream, principal: Principal) -> str:
        return resolve_token(upstream)


# What the engine uses unless a deployment swaps it out.
provider: CredentialProvider = EnvCredentials()
