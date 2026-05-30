#!/usr/bin/env bash
# Put the `yikes` command on your PATH for bash and zsh.
#
# This installs the Poetry venv entrypoint as a symlink in ~/.local/bin and
# makes sure that directory is on PATH from your shell start files. It is
# idempotent: running it again just refreshes the symlink and leaves a single
# guard block in each rc file.
#
# Usage: bash scripts/install-path.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$REPO_ROOT/.venv/bin/yikes"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/yikes"

if [ ! -x "$ENTRYPOINT" ]; then
  echo "yikes entrypoint not found at $ENTRYPOINT" >&2
  echo "Run 'poetry install' in $REPO_ROOT first, then re-run this script." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
ln -sf "$ENTRYPOINT" "$LINK"
echo "linked $LINK -> $ENTRYPOINT"

add_path_guard() {
  local rc="$1"
  local begin="# >>> yikes path >>>"
  local end="# <<< yikes path <<<"
  [ -e "$rc" ] || touch "$rc"
  if grep -qF "$begin" "$rc"; then
    echo "PATH guard already present in $rc"
    return
  fi
  {
    printf '\n%s\n' "$begin"
    printf '%s\n' 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac'
    printf '%s\n' "$end"
  } >> "$rc"
  echo "added PATH guard to $rc"
}

add_path_guard "$HOME/.zshrc"
add_path_guard "$HOME/.bashrc"

echo
echo "Done. Open a new shell, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
echo "Then: yikes --help"
