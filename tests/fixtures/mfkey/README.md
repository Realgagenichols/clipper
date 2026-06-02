# mfkey32 test fixtures

This directory contains `.mfkey32.log` files plus the ground-truth keys that
`flipper_mfkey_crack` is expected to recover. The fixture sweep
(`tests/test_mfkey.py::test_fixture_keys_match_expected`,
`@pytest.mark.regression`) runs every entry in `expected_keys.json` through
the production code path and asserts an exact match.

## License

The Crypto1 LFSR rollback ported in `clipper/hardware/mfkey.py` is derived from
`RfidResearchGroup/proxmark3` (file: `common/crapto1/`, `tools/mfc/card_reader/
mfkey32v2.c`) at SHA `7135bba32450fec1c65936a9b564461ba0ba18b6`, which is
GPL-3.0-or-later. The Python port and these fixtures inherit the same license.

## Provenance

### `synthetic_default_key.log`, `synthetic_transport_key.log`, `synthetic_multi_sector.log`

**Source:** Synthesized in this repo by `clipper.hardware.mfkey._synthesize_capture`,
which runs the Crypto1 *forward* cipher (`_crypto1_init`, `_crypto1_word`,
`prng_successor`) against a chosen `(uid, key, nt, nr)` tuple to produce the
encrypted `nr_enc / ar_enc` values that a real reader+tag pair would have
exchanged. The fixture's expected key is the chosen key — by construction
correct.

**Limitation:** Synthetic fixtures only prove the *rollback* primitives invert
the *forward* primitives correctly within this codebase. They do **NOT** prove
the forward primitives themselves match the on-device firmware's Crypto1
implementation. The R9 spec's "Cryptographic correctness regression coverage"
scenario explicitly calls out that the user-supplied real-hardware capture in
the generator script is the authoritative external oracle.

**Reproducing the fixtures:**

```python
from clipper.hardware.mfkey import _synthesize_capture, _capture_to_log_line

# Example: regenerate synthetic_transport_key.log
cap = _synthesize_capture(
    sector=4, key_half='A', uid=0xcafebabe,
    key=bytes.fromhex('a0a1a2a3a4a5'),
    nt0=0x12345678, nr0=0xdeadbeef,
    nt1=0x87654321, nr1=0xcafef00d,
)
print(_capture_to_log_line(cap))
```

The exact seed tuples are checked in alongside the `.log` files — the byte
contents of every fixture are deterministic given the inputs above.

### `user_*` (added in the generator script)

When the user runs the full cloning workflow against a real door card, the
captured `.mfkey32.log` is committed here with its ground-truth keys recorded
in `expected_keys.json`. That fixture is the EXTERNALLY-VERIFIABLE one — it
exercises the same on-device Crypto1 implementation the production action will
see at runtime. The synthetic fixtures above are necessary but not sufficient.

## `expected_keys.json` shape

```jsonc
{
  "<filename>.log": [
    {"sector": <int>, "key_a": "<12-hex>" | null, "key_b": "<12-hex>" | null},
    ...
  ]
}
```

The list is sorted by sector. Within a sector entry, either `key_a` or
`key_b` (or both) carries the recovered hex; the other half is `null` when
the fixture only contains captures for one half. This shape mirrors the
`flipper_mfkey_crack` action's `keys` return field exactly so the regression
test can `assert result["keys"] == expected` without any reshape glue.
