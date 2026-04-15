#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LINK="/usr/local/bin/owa-piggy"
TARGET="$SCRIPT_DIR/owa-piggy"

if [ -L "$LINK" ] || [ -e "$LINK" ]; then
  echo "$LINK already exists."
  read -p "Overwrite? (y/N): " answer
  [[ "$answer" != "y" && "$answer" != "Y" ]] && exit 0
  rm "$LINK"
fi

ln -s "$TARGET" "$LINK"
echo "Linked: $LINK -> $TARGET"
