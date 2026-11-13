# Private push v1 test vectors

`vectors.json` is the shared contract for the private push envelope described in `docs/reference/private-push-v1.md`. This repository, the iOS app and the relay all test against it.

Every key in it is a test key. Each one is `SHA-256(label)` of the public label stored beside it (for example `istota private push v1 test device key`), so anyone can rederive it, and none is used by any installation, device or relay. The binding, generation, room token and names in the messages are made up.

## Regenerating

```
uv run python tests/fixtures/private_push_v1/generate.py
uv run pytest tests/test_push_envelope.py -q --no-header
```

`generate.py` seals the positive vectors with `istota.push.envelope.seal` and builds each negative vector from correctly signed and encrypted parts, so it fails at the check its `expected_error` names and not earlier. HPKE and the padding are randomized: a run keeps the keys, headers and plaintexts and changes `enc`, the ciphertexts and the signatures. The iOS app and the relay copy the file, so regenerate only when the protocol changes, and update their copies in the same change.

`tests/test_push_envelope.py` opens every positive vector and requires every negative vector to fail with its stated reason. It also checks that the keys match their labels and that the file still covers every padding bucket, multibyte text, the inbox fallback, a null collapse and a null badge.
