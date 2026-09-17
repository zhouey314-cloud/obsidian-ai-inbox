# Verification

Run the local unit tests against temporary vaults:

```bash
python3 -m unittest discover -s . -p 'test_*.py'
python3 app.py --vault demo-vault --check
```

The tests cover append-only capture, duplicate handling, attachment limits,
path safety, search boundaries, token checks and the local HTTP contract. They
do not verify the macOS desktop launcher, browser policy or Obsidian UI.
