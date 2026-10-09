#!/bin/bash
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$APP_DIR/backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%F_%H-%M-%S)
BACKUP_PATH="$BACKUP_DIR/macros_full_backup_$TIMESTAMP.tar.gz"

tar -czf "$BACKUP_PATH" -C "$APP_DIR" main.py macros.db templates static .env
echo "Tam yedek oluşturuldu: $BACKUP_PATH"
