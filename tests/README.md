# Tests

Standard library only, no container, no emulator:

```sh
python3 -m unittest discover -s tests -t tests
```

`support.py` imports `root/root/broker.py` under a throwaway module name with
`PUID`/`PGID` set to the current user, so the module's `chown` calls succeed
without root. Every test points the broker's data directories at a temporary
dir through the `SSTATE_DIR`, `SAVE_DATA_ROOT` and `GCI_CARD_DIR` overrides.

| File | Covers |
|------|--------|
| `test_rom_path.py` | ROM path validation: traversal, symlink escape, prefix siblings |
| `test_ini_patch.py` | `Dolphin.ini` patching: section placement, idempotence, no duplicate headers |
| `test_save_sync.py` | `/save-file`: archive contents, baseline filtering, hostile zip members |
| `test_memory_card.py` | `/memory-card`: whole-card replace, staging swap, survival of a failed hydrate |
| `test_state_files.py` | Savestate discovery and the write-confirmation poll |
| `test_http_api.py` | Live server: routing, auth, slot validation, status codes, round trips |
| `test_hardening.py` | Zip bombs, planted symlinks, unreadable members, spooling, interrupted card swaps |
| `test_process_lifecycle.py` | Crash-relaunch backoff and cap, launch serialisation, the display wait |
| `test_audio_sinks.py` | Recreating the null sinks svc-selkies drops, and where the default sink points |
| `test_packaging.py` | s6 wiring, sudoers coverage, and drift between `init.sh` and `broker.py` |

`test_packaging.py` is the early-warning half. It reads `broker.py` and
`init.sh` as text and fails when the two copies of the Dolphin defaults
disagree, when the broker starts shelling out to a binary sudoers does not
cover, or when a service file stops pointing at a file that exists.
