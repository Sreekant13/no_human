"""The PINNED manifest-signing public keys.

These bytes are the trust anchor for every remote rule this machine will ever
inject into a coder prompt. They are a CONSTANT in the client, changed only by a
product release the operator chooses to install.

WHY PINNED AND NOT FETCHED. The control plane could serve its own public key —
it is public, and there is an obvious route for it. That would be worthless: an
attacker who can rewrite the manifest chain can rewrite the key beside it, and a
key served by the party that signs is a self-attestation rather than a trust
anchor. The client therefore never asks for a key, and there is deliberately no
fallback path that would let it.

WHY A SET. AWS KMS cannot auto-rotate an asymmetric key, so rotation is a
release: ship (current, next), let both verify for one cycle, drop the old one.

WHY NO ARN, NO REGION, NO ACCOUNT ID. The project's lean-stack rule binds the
local product, and ``tests/test_brain_invariants.py`` greps this whole package
for AWS surface. A key is bytes; an ARN is a fact about someone's cloud account. The
bytes are all that verification needs, so the bytes are all that ships. The
fingerprint below is what an auditor compares against the operator's own
``get-public-key`` output.

HOW THE OPERATOR REGENERATES THIS FILE (a manual, auditable step, and it should
stay one — it is the moment a human decides which key this build will trust)::

    aws kms get-public-key --region <region> --key-id <key-id> \\
        --query PublicKey --output text | tr -d '\\n'

paste the base64 below, and check the fingerprint with::

    python -c "import base64,hashlib,sys; \\
        print(hashlib.sha256(base64.b64decode(sys.argv[1])).hexdigest())" <b64>
"""

from __future__ import annotations

import base64

# SubjectPublicKeyInfo DER, base64. RSA-2048, e=65537.
# sha256(DER) = 12a984c342c411e3d38183b0339c20eb977193e35f33877b9685cb2a96d71c6e
_CURRENT_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtHpHhDmkru9Hzc7o5SvX"
    "aND8gMM69W3eL/N3klbJ/or7nSudgkLQLeTlanEsA5Szqz0hmqJu4hvgKCijaRK+"
    "4pgG2+3SkmYTMDa7bz6zse8jxhbzb0m8HBCS69i6FNpANrRoIeGIF4RTcP9AdgKP"
    "x2Xj457xmEWAJnj/gqqtiYpHxhkKTKNpGhvWcQCbhLXhv+b+a6Jhf+fuDcLi62eL"
    "dbWBlU4vgKo0lMjAs+OY7j88nnfWnrwx/DUnj+sJGcfQdHccuOIb02fzEFq2+9Ny"
    "yHtglWMTYNz2tQc2MvSKJNhzzvczMy2sAN1GWOpyoN0VAV3Jte99RVTMsn9EQYVS"
    "EQIDAQAB"
)

#: Every key this build accepts, most-current first. An EMPTY tuple verifies
#: nothing at all, which is the correct fail-closed state for a build that has
#: not pinned a key: zero remote rules is exactly how ``nh`` behaves today.
PINNED_SIGNING_KEYS: tuple[bytes, ...] = (base64.b64decode(_CURRENT_B64),)

#: Published so a user can check what their build trusts without decoding
#: anything: ``nh brain status`` prints these.
PINNED_FINGERPRINTS: tuple[str, ...] = (
    "12a984c342c411e3d38183b0339c20eb977193e35f33877b9685cb2a96d71c6e",
)
