#!/usr/bin/env bash
set -euo pipefail

# Load .env if present (optional)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

REGION="${REGION:-asia-northeast1}"
FUNC_NAME="${FUNC_NAME:-receipt-bot}"
RUNTIME="${RUNTIME:-python311}"

# Enable required services (idempotent)
gcloud services enable cloudfunctions.googleapis.com run.googleapis.com vision.googleapis.com

# Deploy (Gen2 HTTP-triggered function)
gcloud functions deploy "$FUNC_NAME" \
  --gen2 \
  --region="$REGION" \
  --runtime="$RUNTIME" \
  --source=. \
  --entry-point=main \
  --trigger-http \
  --allow-unauthenticated \
  --set-env-vars NOTION_API_KEY="$NOTION_API_KEY",NOTION_ITEMS_DB_ID="$NOTION_ITEMS_DB_ID",NOTION_RECEIPTS_DB_ID="$NOTION_RECEIPTS_DB_ID",LINE_CHANNEL_ACCESS_TOKEN="$LINE_CHANNEL_ACCESS_TOKEN",LINE_CHANNEL_SECRET="$LINE_CHANNEL_SECRET",GEMINI_API_KEY="$GEMINI_API_KEY"
