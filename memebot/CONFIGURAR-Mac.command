#!/usr/bin/env bash
cd "$(dirname "$0")" && docker compose run --rm --no-deps -v "$PWD":/work -w /work bot python -m bot.setup && docker compose restart bot
