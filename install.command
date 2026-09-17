#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -z "${OBSIDIAN_VAULT:-}" ]]; then
  read -r "OBSIDIAN_VAULT?Path to an existing Obsidian vault: "
fi
exec "$(command -v python3)" install_mac.py --vault "$OBSIDIAN_VAULT"
