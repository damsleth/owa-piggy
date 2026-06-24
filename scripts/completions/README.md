# Shell completions for owa-piggy

Tab-completion for the `owa-piggy` subcommands and flags. The files here are
**generated** from the live argparse parser by `scripts/gen-completions.py`, so
they cannot drift from the CLI — `tests/test_completions.py` fails if they go
stale. Regenerate after changing the parser:

```sh
python3 scripts/gen-completions.py
```

## Install

### zsh

`_owa-piggy` is a `#compdef` function. Drop it on your `$fpath` and restart zsh:

```sh
# pick any dir on your fpath, e.g. a personal completions dir
mkdir -p ~/.zsh/completions
cp scripts/completions/_owa-piggy ~/.zsh/completions/
# ensure it's on the fpath (in ~/.zshrc, before `compinit`):
#   fpath=(~/.zsh/completions $fpath)
#   autoload -Uz compinit && compinit
```

### bash

Source the script from your `~/.bashrc` (or drop it in a
`bash-completion` directory):

```sh
cp scripts/completions/owa-piggy.bash ~/.owa-piggy.bash
echo 'source ~/.owa-piggy.bash' >> ~/.bashrc
```

### fish

Fish auto-loads completions from `~/.config/fish/completions/`:

```sh
cp scripts/completions/owa-piggy.fish ~/.config/fish/completions/owa-piggy.fish
```
