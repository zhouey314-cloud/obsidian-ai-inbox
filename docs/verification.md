# Verification

Run the local unit tests against temporary vaults:

```bash
python3 -m unittest discover -s . -p 'test_*.py'
python3 app.py --vault demo-vault --check
```

The tests cover append-only capture, duplicate handling, attachment limits,
path safety, search boundaries, token checks and the local HTTP contract. They
do not verify the macOS desktop launcher, browser policy or Obsidian UI.

`test_batch_import.py` additionally covers two-phase offline import using temporary
synthetic sources and vaults: more than 12 files, provenance/time preservation,
content deduplication, incremental retries, changed-source detection, strict JSON,
per-record failures, encoding/size limits, path boundaries, concurrency and report
failure recovery. See [batch import](batch-import.md). No personal vault or real
ChatGPT/Codex full-history export has been migrated or validated by these tests.

2026-10-30 verification in the Linux workspace: all 104 tests passed (70 existing,
34 batch-import tests); the synthetic demo `--check` returned `writes: 0`.
A separate subprocess CLI check previewed two identical synthetic text files and
one unknown JSON format: first apply reported 1 imported / 1 duplicate / 1 error;
retry reported 2 duplicates / 1 error and left exactly one complete note.

