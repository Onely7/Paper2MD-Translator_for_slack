#!/bin/bash

source ./.env

uv run python open_ngroktunnel.py \
    --port $PORT
