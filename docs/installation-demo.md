# Installation and search demo

Try the local capture-and-search tool with the bundled fictional notes
before pointing it at any personal data. This walkthrough needs Python 3.9+
and no API key, plugin install, or real vault.

## Check the example vault without writing

From the repository root:

```sh
python3 app.py --vault demo-vault --check
```

Look for `ok: true` and `writes: 0` in the JSON output. This checks the
selected path without a live capture or a desktop launcher installation.

## Find the bundled notes

1. Run `python3 app.py --vault demo-vault` to open the local UI; it binds
   to loopback only.
2. Choose **找以前的资料** and search for `Synthetic`. Expect the two
   bundled example notes. Open a result to inspect its local Markdown link.
3. Use the page's **关闭入口** control to stop the running demo. The
   search exercise does not require saving a note or touching an actual vault.

The README screenshot shows this synthetic vault in the running local UI.
The two-result browser check does not verify launcher installation, Obsidian
URI behavior, personal vault compatibility, or sync. This tool does not
upload notes itself; a vault's Obsidian Sync, iCloud, or other separate
synchronization settings are outside the tool.

[Verification scope](verification.md) · [Fixed CI snapshot (`cee3520`)](https://github.com/zhouey314-cloud/obsidian-ai-inbox/actions/runs/39590884846) · [Back to README](../README.md)
