# Installation and search demo

1. Run `python3 app.py --vault demo-vault --check` to verify the bundled synthetic vault without writes.
2. Run `python3 app.py --vault demo-vault` to open the local UI.
3. Select “找以前的资料” and search for a keyword from the synthetic notes in `demo-vault/`.
4. The result links to a local Markdown file; no cloud index or semantic model is used.

The README screenshot was captured from the running local UI with the bundled
synthetic vault selected. In a browser check, searching `Synthetic` returned
the two fixture notes. This does not verify a desktop installation or a private
vault.
