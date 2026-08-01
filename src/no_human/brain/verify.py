"""Signature verification for the team-brain admission manifest — stdlib only.

WHY THIS FILE EXISTS AT ALL, rather than `pip install cryptography`.

The team brain is the one channel that puts text written by somebody else into
this machine's coder prompt. The control plane signs every admission with a KMS
RSA-2048 key (RSASSA-PSS, SHA-256) and chains the manifests by hash, so a
tampered chain is detectable by anyone holding the public half. Verifying that
chain is public-data-only integer arithmetic: ``pow(s, e, n)``, MGF1 over
``hashlib.sha256``, and a byte comparison. No secret material passes through
here and none ever will.

The precedent is the control plane's own ``lambda/rs256.py`` — 68 lines, no
``cryptography``, written for the same reason (the Lambda runtime does not ship
it). Here the reason is the project's lean-stack rule: the local product may
not grow a dependency because the hosted tier exists. ``cryptography`` is a compiled
extension with a Rust toolchain behind its wheels; adding it to make a developer
laptop able to check a signature would be exactly the bleed the constraint
forbids.

Discipline copied from ``rs256.py`` and kept deliberately:

  * return ``False``, never raise, on any malformation — every failure mode
    collapses to one answer and nothing about the input is inferable from which
    exception came back;
  * compare the whole encoded block rather than parsing pieces out of it;
  * the caller supplies the pinned key. This module never fetches one. A key
    served by the party that signs is a self-attestation, not a trust anchor.
"""

from __future__ import annotations

import hashlib
import json

_HASH_LEN = 32  # SHA-256
_SALT_LEN = 32  # AWS KMS RSASSA_PSS_SHA_256 uses salt length == digest length


# --- canonicalisation --------------------------------------------------------


def canonical(payload: dict) -> bytes:
    """Byte-exact serialisation of a manifest, matching the signer.

    The control plane signs the bytes produced by
    ``json.dumps(payload, sort_keys=True, separators=(",", ":"))`` encoded
    UTF-8 (``lambda/manifest.py``). Key order and separators are therefore part
    of the format, not a style choice, and re-canonicalising here is what lets
    the client check that the ``sha256`` in the envelope really is the digest of
    the manifest the envelope carries — rather than trusting a number the same
    envelope supplied.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- minimal strict DER reader ----------------------------------------------
#
# Enough ASN.1 to walk a SubjectPublicKeyInfo and nothing else. Written strict
# and total: every unexpected shape raises ValueError, which the one public
# entry point turns into "this key is unusable" rather than a partial parse.
# There is no attacker-supplied input here — the DER is a constant compiled into
# the client (``keys.py``) — but a parser that guesses is a parser that will be
# reused on something that is.


class _DerError(ValueError):
    pass


def _read_tlv(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """Return (tag, value, next_pos) for the TLV at *pos*."""
    if pos + 2 > len(buf):
        raise _DerError("truncated DER")
    tag = buf[pos]
    first = buf[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4 or pos + n > len(buf):
            raise _DerError("unsupported DER length")
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    if pos + length > len(buf):
        raise _DerError("DER value overruns the buffer")
    return tag, buf[pos:pos + length], pos + length


def _read_int(buf: bytes, pos: int) -> tuple[int, int]:
    tag, value, nxt = _read_tlv(buf, pos)
    if tag != 0x02:
        raise _DerError("expected INTEGER")
    if not value:
        raise _DerError("empty INTEGER")
    if value[0] & 0x80:
        raise _DerError("negative INTEGER in an RSA key")
    return int.from_bytes(value, "big"), nxt


def rsa_public_numbers(spki_der: bytes) -> tuple[int, int]:
    """Extract ``(n, e)`` from a DER SubjectPublicKeyInfo for an RSA key.

    This is the shape ``aws kms get-public-key`` returns (base64 of the DER).
    Raises ValueError on anything that is not an RSA SPKI.
    """
    tag, outer, _ = _read_tlv(spki_der, 0)
    if tag != 0x30:
        raise _DerError("SPKI is not a SEQUENCE")

    tag, algorithm, pos = _read_tlv(outer, 0)
    if tag != 0x30:
        raise _DerError("AlgorithmIdentifier is not a SEQUENCE")
    oid_tag, oid, _ = _read_tlv(algorithm, 0)
    # 1.2.840.113549.1.1.1 — rsaEncryption. Pinned as bytes: an RSA modexp
    # against a key whose algorithm says ECDSA would be a silent nonsense.
    if oid_tag != 0x06 or oid != bytes.fromhex("2a864886f70d010101"):
        raise _DerError("not an rsaEncryption key")

    tag, bitstring, _ = _read_tlv(outer, pos)
    if tag != 0x03 or not bitstring or bitstring[0] != 0x00:
        raise _DerError("subjectPublicKey is not a whole-octet BIT STRING")

    tag, rsa_key, _ = _read_tlv(bitstring[1:], 0)
    if tag != 0x30:
        raise _DerError("RSAPublicKey is not a SEQUENCE")
    n, pos2 = _read_int(rsa_key, 0)
    e, _ = _read_int(rsa_key, pos2)
    if n <= 0 or e <= 0:
        raise _DerError("degenerate RSA parameters")
    return n, e


# --- EMSA-PSS-VERIFY (RFC 8017 §9.1.2) --------------------------------------


def _mgf1(seed: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def verify_pss_sha256(n: int, e: int, digest: bytes, signature: bytes) -> bool:
    """Verify an RSASSA-PSS SHA-256 signature over an ALREADY-HASHED message.

    *digest* is the SHA-256 of the signed bytes. It is passed in rather than
    computed here because that is exactly what the signer signed: KMS is called
    with ``MessageType="DIGEST"`` (``lambda/manifest.py``), so the message whose
    hash PSS encodes is the manifest's canonical bytes and the digest is the
    value the envelope also publishes. The caller checks those agree BEFORE
    calling this; passing the digest keeps the two checks separable.

    Returns False — never raises — on every malformation.
    """
    try:
        if len(digest) != _HASH_LEN:
            return False

        mod_bits = n.bit_length()
        em_bits = mod_bits - 1
        em_len = (em_bits + 7) // 8
        k = (mod_bits + 7) // 8

        if len(signature) != k:
            return False
        if em_len < _HASH_LEN + _SALT_LEN + 2:
            return False

        s = int.from_bytes(signature, "big")
        if s >= n:
            return False

        em = pow(s, e, n).to_bytes(em_len, "big")

        # 0xbc trailer.
        if em[-1] != 0xBC:
            return False

        masked_db = em[: em_len - _HASH_LEN - 1]
        h = em[em_len - _HASH_LEN - 1: em_len - 1]

        # The leftmost 8*emLen - emBits bits of maskedDB must be zero.
        spare_bits = 8 * em_len - em_bits
        if spare_bits and masked_db[0] >> (8 - spare_bits):
            return False

        db_mask = _mgf1(h, em_len - _HASH_LEN - 1)
        db = bytearray(x ^ y for x, y in zip(masked_db, db_mask))
        if spare_bits:
            db[0] &= 0xFF >> spare_bits

        # PS (zeros) || 0x01 || salt
        ps_len = em_len - _HASH_LEN - _SALT_LEN - 2
        if any(db[:ps_len]) or db[ps_len] != 0x01:
            return False
        salt = bytes(db[ps_len + 1:])
        if len(salt) != _SALT_LEN:
            return False

        h_prime = hashlib.sha256(b"\x00" * 8 + digest + salt).digest()
    except Exception:  # noqa: BLE001 — see the module docstring: one answer.
        return False

    # Constant-time by construction: both sides are public, but comparing
    # digests instead of raw bytes removes the question, as rs256.py does.
    return hashlib.sha256(h).digest() == hashlib.sha256(h_prime).digest()


def verify_with_pinned_keys(
    pinned: "tuple[bytes, ...] | list[bytes]",
    digest: bytes,
    signature: bytes,
) -> bool:
    """True when *signature* verifies under ANY pinned SPKI DER.

    A set rather than one key because KMS cannot auto-rotate an asymmetric key,
    so rotation is a product release: the client ships current + next and both
    are accepted for one release cycle. An empty set verifies NOTHING, which is
    the correct fail-closed answer for a build that has not pinned a key — zero
    remote rules is how ``nh`` behaves today.
    """
    for spki_der in pinned:
        try:
            n, e = rsa_public_numbers(spki_der)
        except ValueError:
            continue
        if verify_pss_sha256(n, e, digest, signature):
            return True
    return False
