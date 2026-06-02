# SPDX-License-Identifier: GPL-3.0-or-later
"""clipper.hardware.mfkey — Pure-Python mfkey32v2 Crypto1 key recovery.

This module is the host-side cryptographic engine that turns a Flipper-captured
`.mfkey32.log` file into recovered Mifare Classic 6-byte keys. No hardware I/O
happens here — the only Flipper interaction is reading the `.mfkey32.log` text
via the existing `flipper_storage_read` action; the LFSR rollback runs entirely
in this process.

License: GPL-3.0-or-later. The Crypto1 LFSR helpers below are direct ports of
the C implementation in `RfidResearchGroup/proxmark3` (file: `common/crapto1/`
+ `tools/mfc/card_reader/mfkey32v2.c`) at pinned SHA
`7135bba32450fec1c65936a9b564461ba0ba18b6`, which is GPL-3.0-or-later. This
Python port inherits the upstream license.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass

from pydantic import BaseModel

from clipper.actions import Action, EmissionBlocked, register
from clipper.safety import safety_allowed

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# _MFKEY32_LOG_FORMAT — verbatim upstream citations
# ---------------------------------------------------------------------------
#
# Two upstream artifacts pin this module's behaviour:
#
#   1. The Flipper firmware that PRODUCES the `.mfkey32.log` text. This is
#      the source of truth for the line format we must parse.
#
#        Source: Next-Flip/Momentum-Firmware tag mntm-012
#          tag SHA:   e1784e7418d8b074e971983ceb6fef0f37e52ae4
#          file:      applications/main/nfc/helpers/mfkey32_logger.c
#
#   2. The iceman1001 mfkey32v2 reference implementation we port the
#      Crypto1 LFSR rollback from, vendored inside RfidResearchGroup/proxmark3.
#
#        pinned SHA: 7135bba32450fec1c65936a9b564461ba0ba18b6
#        License: GPL-3.0-or-later → derivative below.
#
# Log line format (from mfkey32_logger.c L128-L139):
#
#   Sec %d key %c cuid %08lx nt0 %08lx nr0 %08lx ar0 %08lx
#   nt1 %08lx nr1 %08lx ar1 %08lx\n
#
# Single ASCII space separators, lowercase zero-padded 8-hex for every
# uint32_t, decimal sector number, single-char A|B for key half. Every
# emitted line carries six nonces (mfkey32_logger.c L60-L75 + L122-L143:
# `is_filled` is only true after the SECOND auth pair completes).


# ---------------------------------------------------------------------------
# Constants — pinned to proxmark3 crapto1.h
# ---------------------------------------------------------------------------

# common/crapto1/crapto1.h L62-L63
_LF_POLY_ODD = 0x29CE5C
_LF_POLY_EVEN = 0x870804


# ---------------------------------------------------------------------------
# Bit-tools
# ---------------------------------------------------------------------------


def _bit(x: int, n: int) -> int:
    """common/crapto1/crapto1.h L64: #define BIT(x, n) ((x) >> (n) & 1)."""
    return (x >> n) & 1


def _bebit(x: int, n: int) -> int:
    """common/crapto1/crapto1.h L65: #define BEBIT(x, n) BIT(x, (n) ^ 24)."""
    return _bit(x, n ^ 24)


def _evenparity32(x: int) -> int:
    """common/parity.h L74-L82: even parity over 32 bits.

    Returns the XOR of all 32 bits of *x* (0 or 1). Equivalent to
    __builtin_parity on a uint32.
    """
    x &= 0xFFFFFFFF
    x ^= x >> 16
    x ^= x >> 8
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


# Speed: 16-bit parity LUT lets the hot inner loops fold a uint32 down
# in three table lookups + two XORs instead of the bit-shift cascade.
_PARITY16: list[int] = [(bin(i).count("1")) & 1 for i in range(0x10000)]


def _ep32(x: int) -> int:
    """Fast evenparity32 via 16-bit LUT — same semantics as _evenparity32."""
    return _PARITY16[(x ^ (x >> 16)) & 0xFFFF]


# ---------------------------------------------------------------------------
# Crypto1 filter (the f-function in MIFARE Classic Crypto1)
# ---------------------------------------------------------------------------


def crypto1_filter(x: int) -> int:
    """The Crypto1 ``f`` filter function — 1 bit out of 20 LFSR bits.

    Verbatim port of common/crapto1/crapto1.h L69-L78 (the inline non-LUT
    variant). The five 16-bit constants ``0xf22c0`` etc. encode the
    truth tables of f_a/f_b/f_c (Garcia et al., "Dismantling MIFARE
    Classic", ESORICS 2008), and 0xEC57E80A is the f_c output table.
    """
    f = 0xF22C0 >> (x & 0xF) & 16
    f |= 0x6C9C0 >> (x >> 4 & 0xF) & 8
    f |= 0x3C8B0 >> (x >> 8 & 0xF) & 4
    f |= 0x1E458 >> (x >> 12 & 0xF) & 2
    f |= 0x0D938 >> (x >> 16 & 0xF) & 1
    return _bit(0xEC57E80A, f)


# Module-level filter LUT — proxmark3's crapto1.c L36-L40 builds the same
# `filterlut[0x100000]` at constructor time and #defines filter(x) →
# filterlut[(x) & 0xfffff]. We follow the same pattern for speed.
_FILTERLUT: list[int] = [crypto1_filter(i) for i in range(0x100000)]


def _filt(x: int) -> int:
    """Filter via precomputed LUT (mirrors crapto1.c L56 ``#define filter``)."""
    return _FILTERLUT[x & 0xFFFFF]


# ---------------------------------------------------------------------------
# PRNG / forward cipher
# ---------------------------------------------------------------------------


def prng_successor(x: int, n: int) -> int:
    """common/crapto1/crypto1.c L149-L155 — Mifare PRNG forward step.

    `prng_successor(nt, 64)` is the plaintext value of ar that the reader
    sends in response to tag nonce nt. The Flipper's log records the
    *encrypted* ar (ar0/ar1); XORing it against prng_successor(nt0, 64)
    yields ks2, the 32 bits of keystream that drive lfsr_recovery32.
    """
    x &= 0xFFFFFFFF
    # SWAPENDIAN(x) — crypto1.c L34-L35
    x = ((x >> 8) & 0x00FF00FF) | ((x & 0x00FF00FF) << 8)
    x = ((x >> 16) | (x << 16)) & 0xFFFFFFFF
    for _ in range(n):
        # crypto1.c L152
        bit = (x >> 16) ^ (x >> 18) ^ (x >> 19) ^ (x >> 21)
        x = ((x >> 1) | ((bit & 1) << 31)) & 0xFFFFFFFF
    # second SWAPENDIAN
    x = ((x >> 8) & 0x00FF00FF) | ((x & 0x00FF00FF) << 8)
    x = ((x >> 16) | (x << 16)) & 0xFFFFFFFF
    return x


@dataclass
class _Crypto1State:
    """common/crapto1/crapto1.h L24: struct Crypto1State {uint32_t odd, even;}."""

    odd: int = 0
    even: int = 0


def _crypto1_init(state: _Crypto1State, key: int) -> None:
    """common/crapto1/crypto1.c L37-L47 — load a 48-bit key into the LFSR."""
    state.odd = 0
    state.even = 0
    # for (int i = 47; i > 0; i -= 2)
    i = 47
    while i > 0:
        state.odd = ((state.odd << 1) | _bit(key, (i - 1) ^ 7)) & 0xFFFFFFFF
        state.even = ((state.even << 1) | _bit(key, i ^ 7)) & 0xFFFFFFFF
        i -= 2


def _crypto1_get_lfsr(state: _Crypto1State) -> int:
    """common/crapto1/crypto1.c L69-L75 — extract 48-bit key from LFSR state."""
    lfsr = 0
    for i in range(23, -1, -1):
        lfsr = (lfsr << 1) | _bit(state.odd, i ^ 3)
        lfsr = (lfsr << 1) | _bit(state.even, i ^ 3)
    return lfsr & 0xFFFFFFFFFFFF


def _crypto1_bit(s: _Crypto1State, in_bit: int, is_encrypted: int) -> int:
    """common/crapto1/crypto1.c L76-L91 — one cipher step (forward)."""
    ret = _filt(s.odd)
    feedin = ret & (1 if is_encrypted else 0)
    feedin ^= 1 if in_bit else 0
    feedin ^= _LF_POLY_ODD & s.odd
    feedin ^= _LF_POLY_EVEN & s.even
    s.even = ((s.even << 1) | _evenparity32(feedin)) & 0xFFFFFFFF
    # swap odd/even
    s.odd, s.even = s.even, s.odd
    return ret


def _crypto1_word(s: _Crypto1State, in_word: int, is_encrypted: int) -> int:
    """common/crapto1/crypto1.c L104-L143 — 32-bit cipher step (forward)."""
    ret = 0
    for i in range(32):
        # BEBIT(in, i): bit (i ^ 24) of in
        ret |= _crypto1_bit(s, _bebit(in_word, i), is_encrypted) << ((24 ^ i) & 31)
    return ret & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# LFSR rollback (reverse step)
# ---------------------------------------------------------------------------


def _lfsr_rollback_bit(s: _Crypto1State, in_bit: int, fb: int) -> int:
    """common/crapto1/crapto1.c L329-L345 — one cipher step in reverse.

    Restores the previous state and returns the keystream bit that was
    output at that step. The fb parameter selects whether the keystream
    fed back into the LFSR (is_encrypted in the forward direction).
    """
    s.odd &= 0xFFFFFF
    # swap odd <-> even
    s.odd, s.even = s.even, s.odd

    out = s.even & 1
    s.even >>= 1
    out ^= _LF_POLY_EVEN & s.even
    out ^= _LF_POLY_ODD & s.odd
    out ^= 1 if in_bit else 0
    ret = _filt(s.odd)
    out ^= ret & (1 if fb else 0)

    s.even |= _evenparity32(out) << 23
    s.even &= 0xFFFFFFFF
    return ret


def _lfsr_rollback_word(s: _Crypto1State, in_word: int, fb: int) -> int:
    """common/crapto1/crapto1.c L364-L405 — 32-bit reverse cipher step."""
    ret = 0
    for i in range(31, -1, -1):
        bit = _lfsr_rollback_bit(s, _bebit(in_word, i), fb)
        ret |= bit << ((24 ^ i) & 31)
    return ret & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# lfsr_recovery32 — the central state-recovery routine
# ---------------------------------------------------------------------------


def _extend_table_simple(tbl: list[int], bit_target: int) -> list[int]:
    """common/crapto1/crapto1.c L97-L110 — extend by one keystream bit.

    Python flavour: we read tbl as the input list and emit a fresh out list,
    matching the in-place C semantics functionally without manual pointer
    arithmetic.
    """
    out: list[int] = []
    append = out.append
    for state in tbl:
        s_shift = (state << 1) & 0xFFFFFFFF
        # filter() masks to 20 bits internally — mirror crapto1.c L56.
        f0 = _FILTERLUT[s_shift & 0xFFFFF]
        f1 = _FILTERLUT[(s_shift | 1) & 0xFFFFF]
        if f0 ^ f1:
            # replace: pick the variant whose filter == bit_target
            append(s_shift | (f0 ^ bit_target))
        elif f0 == bit_target:
            # insert: both variants are valid
            append(s_shift)
            append(s_shift | 1)
        # else: drop both
    return out


def _update_contribution(item: int, mask1: int, mask2: int) -> int:
    """common/crapto1/crapto1.c L63-L68 — pack the next two contribution bits
    into the MSB of *item* so that bucket_sort_intersect can find odd/even
    states whose top contribution bytes match.

    p = item >> 25
    p = (p << 1) | evenparity32(item & mask1)
    p = (p << 1) | evenparity32(item & mask2)
    item = (p << 24) | (item & 0xffffff)
    """
    p = item >> 25
    p = (p << 1) | _ep32(item & mask1)
    p = (p << 1) | _ep32(item & mask2)
    return ((p << 24) | (item & 0xFFFFFF)) & 0xFFFFFFFF


def _extend_table(tbl: list[int], bit_target: int, m1: int, m2: int, in_word: int) -> list[int]:
    """common/crapto1/crapto1.c L73-L92 — extend with contribution + input.

    Used in the recursive ``recover`` phase where each shift also folds in
    the corresponding 2-bit slice of the *in* parameter (the byte-swapped
    UID-XOR-nonce that was clocked into the LFSR alongside the keystream).
    """
    in_word = (in_word << 24) & 0xFFFFFFFF
    out: list[int] = []
    append = out.append
    lut = _FILTERLUT
    parity = _PARITY16
    for state in tbl:
        s_shift = (state << 1) & 0xFFFFFFFF
        f0 = lut[s_shift & 0xFFFFF]
        f1 = lut[(s_shift | 1) & 0xFFFFF]
        if f0 ^ f1:
            picked = s_shift | (f0 ^ bit_target)
            # inline _update_contribution
            p = picked >> 25
            v = picked & m1
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            v = picked & m2
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            picked = (((p << 24) | (picked & 0xFFFFFF)) & 0xFFFFFFFF) ^ in_word
            append(picked & 0xFFFFFFFF)
        elif f0 == bit_target:
            v0 = s_shift
            v1 = s_shift | 1
            # inline update for v0
            p = v0 >> 25
            v = v0 & m1
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            v = v0 & m2
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            v0 = (((p << 24) | (v0 & 0xFFFFFF)) & 0xFFFFFFFF) ^ in_word
            # inline update for v1
            p = v1 >> 25
            v = v1 & m1
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            v = v1 & m2
            p = (p << 1) | parity[(v ^ (v >> 16)) & 0xFFFF]
            v1 = (((p << 24) | (v1 & 0xFFFFFF)) & 0xFFFFFFFF) ^ in_word
            append(v0 & 0xFFFFFFFF)
            append(v1 & 0xFFFFFFFF)
        # else: drop
    return out


def _bucket_intersect(
    e_tbl: list[int], o_tbl: list[int]
) -> list[tuple[list[int], list[int]]]:
    """common/bucketsort.c — bucket the two lists by the top-8 contribution
    bits and yield the per-bucket (even, odd) pairs where BOTH buckets are
    non-empty. This is the search-space pruning that makes lfsr_recovery32
    finish in human time.
    """
    e_buckets: dict[int, list[int]] = {}
    o_buckets: dict[int, list[int]] = {}
    for v in e_tbl:
        b = (v >> 24) & 0xFF
        e_buckets.setdefault(b, []).append(v)
    for v in o_tbl:
        b = (v >> 24) & 0xFF
        o_buckets.setdefault(b, []).append(v)
    pairs: list[tuple[list[int], list[int]]] = []
    for b in sorted(e_buckets.keys()):
        if b in o_buckets:
            pairs.append((e_buckets[b], o_buckets[b]))
    return pairs


def _recover(
    o_tbl: list[int],
    oks: int,
    e_tbl: list[int],
    eks: int,
    rem: int,
    in_word: int,
    out_states: list[_Crypto1State],
) -> None:
    """common/crapto1/crapto1.c L113-L154 — recursive narrowing.

    At each level we consume one bit of oks/eks (and two bits of in_word
    that get clocked into the matching LFSR halves). When rem == -1 we
    pair up surviving odd/even candidates and emit them as Crypto1State
    records.
    """
    if rem == -1:
        # crapto1.c L120-L130
        in_bit = 1 if (in_word & 4) else 0
        for e in e_tbl:
            e2 = ((e << 1) ^ _ep32(e & _LF_POLY_EVEN) ^ in_bit) & 0xFFFFFFFF
            for o in o_tbl:
                state = _Crypto1State()
                state.even = o
                state.odd = (e2 ^ _ep32(o & _LF_POLY_ODD)) & 0xFFFFFFFF
                out_states.append(state)
        return

    # C: `for (i = 0; i < 4 && rem--; i++)` — post-decrement means rem is
    # decremented EVERY iteration whose `i < 4` check passes, but the body
    # only runs when the pre-decrement value was non-zero. Final loop-exit
    # state: rem -= min(4, original_rem + 1).
    rem_local = rem
    for _ in range(4):
        # Mirror C `rem--` truthiness check before decrement.
        if rem_local == 0:
            rem_local -= 1
            break
        rem_local -= 1
        oks >>= 1
        eks >>= 1
        in_word >>= 2
        o_tbl = _extend_table(o_tbl, oks & 1, (_LF_POLY_EVEN << 1) | 1, _LF_POLY_ODD << 1, 0)
        if not o_tbl:
            return
        e_tbl = _extend_table(e_tbl, eks & 1, _LF_POLY_ODD, (_LF_POLY_EVEN << 1) | 1, in_word & 3)
        if not e_tbl:
            return

    for e_bucket, o_bucket in _bucket_intersect(e_tbl, o_tbl):
        _recover(o_bucket, oks, e_bucket, eks, rem_local, in_word, out_states)


def lfsr_recovery32(ks2: int, in_word: int) -> list[_Crypto1State]:
    """common/crapto1/crapto1.c L163-L229 — recover LFSR state from 32 ks bits.

    Returns a list of candidate Crypto1State records that could have
    produced the observed 32 keystream bits. The mfkey32v2 orchestration
    in recover_key_from_capture() then filters that list against the
    second auth pair to identify the unique correct state.
    """
    # Split keystream into odd and even halves (16 bits each). The BEBIT
    # ordering matches crapto1.c L170-L173 verbatim.
    oks = 0
    for i in range(31, -1, -2):
        oks = (oks << 1) | _bebit(ks2, i)
    eks = 0
    for i in range(30, -1, -2):
        eks = (eks << 1) | _bebit(ks2, i)

    # Seed lists with every 20-bit state whose filter matches the rightmost
    # ks bit (crapto1.c L198-L208).
    oks_b1 = oks & 1
    eks_b1 = eks & 1
    odd_tbl: list[int] = []
    even_tbl: list[int] = []
    # crapto1.c L202: `for (i = 1 << 20; i >= 0; --i)` — note `i` can equal
    # 1<<20 here, but the `filter` macro masks to 20 bits (`(x) & 0xfffff`),
    # so the iteration is over states 0..0x100000 with filter(0x100000)
    # aliasing to filter(0). We mirror that via the masked LUT access.
    for i in range((1 << 20) + 1):
        f = _FILTERLUT[i & 0xFFFFF]
        if f == oks_b1:
            odd_tbl.append(i)
        if f == eks_b1:
            even_tbl.append(i)

    # Extend by 4 bits each side (crapto1.c L211-L214).
    for _ in range(4):
        oks >>= 1
        eks >>= 1
        odd_tbl = _extend_table_simple(odd_tbl, oks & 1)
        even_tbl = _extend_table_simple(even_tbl, eks & 1)

    # Byte-swap `in` to match the order LFSR sees it (crapto1.c L219).
    in_word = (
        ((in_word >> 16) & 0xFF)
        | ((in_word << 16) & 0xFFFF0000)
        | (in_word & 0xFF00)
    ) & 0xFFFFFFFF

    out_states: list[_Crypto1State] = []
    _recover(odd_tbl, oks, even_tbl, eks, 11, (in_word << 1) & 0xFFFFFFFF, out_states)
    return out_states


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MfkeyCapture:
    """One six-nonce capture record from a `.mfkey32.log` line.

    Fields mirror the firmware's `Sec / key / cuid / nt0 / nr0 / ar0 /
    nt1 / nr1 / ar1` line format. Integers are 32-bit unsigned.
    """

    sector: int
    key_half: str  # 'A' or 'B'
    uid: int
    nt0: int
    nr0: int
    ar0: int
    nt1: int
    nr1: int
    ar1: int


_TOKENS_ORDER = ("Sec", "key", "cuid", "nt0", "nr0", "ar0", "nt1", "nr1", "ar1")


def parse_mfkey32_log(text: str) -> list[MfkeyCapture]:
    """Parse a `.mfkey32.log` blob into MfkeyCapture records.

    Empty / whitespace-only input returns an empty list (not an error —
    scenario "Empty log").

    Malformed lines raise ValueError citing the offending line; the
    Action.invoke wrapper translates that to ActionRuntimeError.
    """
    captures: list[MfkeyCapture] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        # Expect exactly 18 tokens: 9 labels + 9 values.
        if len(parts) != 18:
            raise ValueError(
                f"mfkey32 log line has {len(parts)} tokens, expected 18: {raw_line!r}"
            )
        # Validate label positions match the firmware's printf format.
        labels_actual = (parts[0], parts[2], parts[4], parts[6], parts[8],
                         parts[10], parts[12], parts[14], parts[16])
        if labels_actual != _TOKENS_ORDER:
            raise ValueError(
                f"mfkey32 log line labels do not match expected sequence "
                f"{_TOKENS_ORDER}: got {labels_actual} in {raw_line!r}"
            )
        try:
            sector = int(parts[1], 10)
            key_half = parts[3]
            if key_half not in ("A", "B"):
                raise ValueError(f"key half must be 'A' or 'B', got {key_half!r}")
            uid = int(parts[5], 16)
            nt0 = int(parts[7], 16)
            nr0 = int(parts[9], 16)
            ar0 = int(parts[11], 16)
            nt1 = int(parts[13], 16)
            nr1 = int(parts[15], 16)
            ar1 = int(parts[17], 16)
        except ValueError as exc:
            raise ValueError(
                f"mfkey32 log line contains invalid integer literal: "
                f"{raw_line!r} ({exc})"
            ) from exc
        captures.append(MfkeyCapture(
            sector=sector, key_half=key_half, uid=uid,
            nt0=nt0, nr0=nr0, ar0=ar0,
            nt1=nt1, nr1=nr1, ar1=ar1,
        ))
    return captures


# ---------------------------------------------------------------------------
# Per-capture key recovery — mirrors tools/mfc/card_reader/mfkey32v2.c L63-L77
# ---------------------------------------------------------------------------


def recover_key_from_capture(c: MfkeyCapture) -> bytes | None:
    """Recover the 6-byte Mifare Classic sector key from one capture record.

    Orchestrates the proxmark3 mfkey32v2 algorithm:
      1. ks2_0 = ar0_enc XOR prng_successor(nt0, 64)
         ks2_1 = ar1_enc XOR prng_successor(nt1, 64)
      2. Recover candidate LFSR states from ks2_0 via lfsr_recovery32.
      3. For each candidate: roll back through (zero_word, nr0_enc,
         uid^nt0) to recover the original key load, then forward-encrypt
         (uid^nt1, nr1_enc, zero_word) and check whether the third output
         word equals ks2_1. The unique survivor is the correct key.

    Returns 6 bytes (key, big-endian) or None if no candidate verified.
    """
    ar0_plain = prng_successor(c.nt0, 64)
    ar1_plain = prng_successor(c.nt1, 64)
    ks2_0 = (c.ar0 ^ ar0_plain) & 0xFFFFFFFF
    ks2_1 = (c.ar1 ^ ar1_plain) & 0xFFFFFFFF

    candidates = lfsr_recovery32(ks2_0, 0)
    if not candidates:
        return None

    uid_xor_nt0 = (c.uid ^ c.nt0) & 0xFFFFFFFF
    uid_xor_nt1 = (c.uid ^ c.nt1) & 0xFFFFFFFF

    for state in candidates:
        # tools/mfc/card_reader/mfkey32v2.c L65-L77
        st = _Crypto1State(odd=state.odd, even=state.even)
        _lfsr_rollback_word(st, 0, 0)
        _lfsr_rollback_word(st, c.nr0, 1)
        _lfsr_rollback_word(st, uid_xor_nt0, 0)
        key_int = _crypto1_get_lfsr(st)

        # Verify against the second auth pair.
        check = _Crypto1State(odd=st.odd, even=st.even)
        _crypto1_word(check, uid_xor_nt1, 0)
        _crypto1_word(check, c.nr1, 1)
        candidate_ks2_1 = _crypto1_word(check, 0, 0)
        if candidate_ks2_1 == ks2_1:
            return key_int.to_bytes(6, "big")
    return None


# ---------------------------------------------------------------------------
# Action — flipper_mfkey_crack
# ---------------------------------------------------------------------------


class MfkeyCrackParams(BaseModel):
    """Inputs for `flipper_mfkey_crack`.

    log_content_b64 — base64-encoded contents of a `.mfkey32.log` file
    captured by the Flipper. Required; no default is meaningful here.
    """

    log_content_b64: str


def _crack_captures(
    captures: list[MfkeyCapture],
) -> tuple[dict[tuple[int, str, str], bytes], int]:
    """Synchronous, CPU-bound: walk captures, dedup, recover keys.

    Runs in a worker thread (see ``_mfkey_crack_handler``) so the asyncio
    event loop stays responsive while the per-capture lfsr_recovery32
    work proceeds. Returns ``(found_per_id, captures_skipped)`` where
    ``found_per_id`` maps ``(sector, key_half, cuid)`` to the recovered
    6-byte key.
    """
    found_per_id: dict[tuple[int, str, str], bytes] = {}
    captures_skipped = 0
    for c in captures:
        key_id = (c.sector, c.key_half, c.uid)
        if key_id in found_per_id:
            captures_skipped += 1
            continue
        key = recover_key_from_capture(c)
        if key is not None:
            found_per_id[key_id] = key
    return found_per_id, captures_skipped


async def _mfkey_crack_handler(_flipper, params: MfkeyCrackParams) -> dict:
    """Recover Mifare Classic sector keys from a `.mfkey32.log` blob.

    Pure host-side computation — no Flipper round-trip. Still safety-gated:
    key recovery is a sensitive capability as a safety-gated capability.
    """
    if not safety_allowed():
        raise EmissionBlocked("flipper_mfkey_crack")

    try:
        log_text = base64.b64decode(params.log_content_b64).decode(
            "utf-8", errors="replace"
        )
    except Exception as exc:
        raise RuntimeError(f"log_content_b64 is not valid base64: {exc}") from exc

    captures = parse_mfkey32_log(log_text)  # may raise ValueError → ActionRuntimeError
    if not captures:
        return {
            "keys": [],
            "stats": {
                "captures_parsed": 0,
                "captures_skipped": 0,
                "sectors_recovered": 0,
            },
        }

    # Performance: lfsr_recovery32 is ~5s per capture in pure Python and
    # the work is CPU-bound. Running it directly here would block the
    # asyncio event loop for the duration — every other in-flight tool call
    # (e.g. a concurrent storage_read) would be starved. Offload to a thread
    # via run_in_executor so concurrent requests stay responsive. The crack
    # still takes its full wall-clock time, but the rest of the server keeps
    # serving.
    #
    # Dedup: for a cloning workflow you only need ONE successful recovery
    # per (sector, key_half, cuid). Skip subsequent captures for the same
    # triple. cuid is part of the key because different cards have
    # different keys for the same (sector, half) — a multi-card log must
    # not cross-pollinate.
    loop = asyncio.get_running_loop()
    found_per_id, captures_skipped = await loop.run_in_executor(
        None, _crack_captures, captures
    )

    # Aggregate per (sector, key_half) — one entry per sector with both
    # halves populated when both were recovered. Multiple cuids' keys for
    # the same (sector, half) become a set; conflict is logged.
    found: dict[tuple[int, str], set[bytes]] = {}
    for (sector, half, _uid), key in found_per_id.items():
        found.setdefault((sector, half), set()).add(key)

    result_keys: list[dict] = []
    for (sector, half), key_set in sorted(found.items()):
        if len(key_set) > 1:
            log.warning(
                "mfkey: conflicting keys recovered for sector %d half %s: %s",
                sector, half, [k.hex() for k in key_set],
            )
        key_hex = next(iter(key_set)).hex()
        entry = next((e for e in result_keys if e["sector"] == sector), None)
        if entry is None:
            entry = {"sector": sector, "key_a": None, "key_b": None}
            result_keys.append(entry)
        entry[f"key_{half.lower()}"] = key_hex
    result_keys.sort(key=lambda e: e["sector"])

    return {
        "keys": result_keys,
        "stats": {
            "captures_parsed": len(captures),
            "captures_skipped": captures_skipped,
            "sectors_recovered": len({s for (s, _h) in found}),
        },
    }


register(Action(
    name="flipper_mfkey_crack",
    description=(
        "Recover Mifare Classic sector keys from a .mfkey32.log file captured "
        "by the Flipper. Pure host-side computation — no device interaction. "
        "Pass the log file's bytes as log_content_b64 (base64). Returns the "
        "list of recovered keys keyed by sector and half (A/B), plus stats. "
        "Safety-gated: requires the SAFETY toggle to be ON (or CLIPPER_SAFETY=1) "
        "even though no RF is emitted — key recovery is a sensitive capability."
    ),
    params=MfkeyCrackParams,
    handler=_mfkey_crack_handler,
    emissive=False,
))


# ---------------------------------------------------------------------------
# Internal: synthetic capture generator (for fixture creation + smoke tests)
# ---------------------------------------------------------------------------


def _synthesize_capture(
    *, sector: int, key_half: str, uid: int, key: bytes,
    nt0: int, nr0: int, nt1: int, nr1: int,
) -> MfkeyCapture:
    """Generate a self-consistent MfkeyCapture for a chosen key + nonces.

    Runs the Crypto1 forward cipher (the same primitives the recovery
    routine inverts) to compute the encrypted nr/ar values that a real
    reader+tag pair would have exchanged given the inputs. Used by the
    fixture generator script — NOT part of the runtime action surface.
    """
    if len(key) != 6:
        raise ValueError("key must be 6 bytes")
    key_int = int.from_bytes(key, "big")

    def _one_pair(nt: int, nr: int) -> tuple[int, int]:
        s = _Crypto1State()
        _crypto1_init(s, key_int)
        _crypto1_word(s, (uid ^ nt) & 0xFFFFFFFF, 0)
        # nr_enc = nr XOR keystream for nr
        nr_enc = _crypto1_word(s, nr, 0) ^ nr
        # plaintext ar = prng_successor(nt, 64)
        ar_plain = prng_successor(nt, 64)
        ar_enc = _crypto1_word(s, 0, 0) ^ ar_plain
        return nr_enc & 0xFFFFFFFF, ar_enc & 0xFFFFFFFF

    nr0_enc, ar0_enc = _one_pair(nt0, nr0)
    nr1_enc, ar1_enc = _one_pair(nt1, nr1)
    return MfkeyCapture(
        sector=sector, key_half=key_half, uid=uid,
        nt0=nt0, nr0=nr0_enc, ar0=ar0_enc,
        nt1=nt1, nr1=nr1_enc, ar1=ar1_enc,
    )


def _capture_to_log_line(c: MfkeyCapture) -> str:
    """Render an MfkeyCapture back into the firmware's printf line format."""
    return (
        f"Sec {c.sector} key {c.key_half} cuid {c.uid:08x} "
        f"nt0 {c.nt0:08x} nr0 {c.nr0:08x} ar0 {c.ar0:08x} "
        f"nt1 {c.nt1:08x} nr1 {c.nr1:08x} ar1 {c.ar1:08x}"
    )
