#!/usr/bin/env sh
set -eu

if [ -d .git ]; then
  echo "Git metadata already exists; no changes made."
  exit 0
fi

git init -b main
git add .
git commit -m "feat: initialize syco23 setcrawler"
echo "Local repository initialized. Add a private remote before pushing."
