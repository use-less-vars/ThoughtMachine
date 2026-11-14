#!/bin/bash
# Install the tracked git hooks into THIS checkout's real hooks directory.
#
# Worktrees do not have a .git/hooks directory -- .git is a file pointing at
# <common-dir>/worktrees/<name> -- so the hooks path is always resolved with
# `git rev-parse --git-path hooks` (which also honours core.hooksPath).
#
# Idempotent: re-running replaces the installed hook with the tracked one and
# prints exactly what it installed.
set -eu

repo_root=$(git rev-parse --show-toplevel)
hooks_rel=$(git rev-parse --git-path hooks)
case "$hooks_rel" in
    /*) hooks_dir="$hooks_rel" ;;
    *)  hooks_dir="$repo_root/$hooks_rel" ;;
esac

src="$repo_root/resources/githooks/pre-commit"
if [ ! -f "$src" ]; then
    echo "install_git_hooks: REFUSED -- missing tracked hook $src" >&2
    exit 1
fi

mkdir -p "$hooks_dir"
install -m 0755 "$src" "$hooks_dir/pre-commit"
echo "install_git_hooks: installed $src -> $hooks_dir/pre-commit"
