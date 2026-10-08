#!/usr/bin/env bash
# Sunucuya dağıtım: commit'lenmiş kodu (HEAD) SSH ile gönderir, imajı sunucuda derler, botu yeniden başlatır.
#
#   DEPLOY_HOST=root@1.2.3.4 DEPLOY_KEY=~/.ssh/key scripts/deploy.sh          # kod + derleme + yeniden başlatma
#   DEPLOY_HOST=root@1.2.3.4 DEPLOY_KEY=~/.ssh/key scripts/deploy.sh --env    # .env'i de (yeniden) gönder
#   ... scripts/deploy.sh --no-start                                          # sadece gönder ve derle, başlatma
#
# Gizli bilgiler (.env) git'e ya da başka bir servise uğramaz: SSH tünelinden doğrudan sunucuya yazılır,
# sadece root okuyabilir (umask 077). Kod GitHub'dan değil, yerel repodan gider; sunucuda git erişimi gerekmez.
set -euo pipefail

HOST="${DEPLOY_HOST:?DEPLOY_HOST ver (ör. root@1.2.3.4)}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/id_ed25519}"
DIR="${DEPLOY_DIR:-/opt/ekampus}"
case "$DIR" in /*/*) ;; *) echo "DEPLOY_DIR en az iki seviyeli mutlak yol olmalı (ör. /opt/ekampus): $DIR" >&2; exit 1 ;; esac
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")
SEND_ENV=0; START=1
for arg in "$@"; do
  case "$arg" in
    --env) SEND_ENV=1 ;;
    --no-start) START=0 ;;
    *) echo "Bilinmeyen seçenek: $arg" >&2; exit 1 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
  echo "Uyarı: commit'lenmemiş değişiklikler gönderilmeyecek; sadece HEAD gider."
fi
echo "Kod gönderiliyor: $(git log -1 --format='%h %s')"

# Yeni sürümü geçici klasöre aç, varsa .env'i taşı, sonra eskisinin yerine koy (silinen dosyalar sunucuda kalmasın)
git -c core.autocrlf=false archive --format=tar HEAD | "${SSH[@]}" "set -e
  tmp=\$(mktemp -d '$DIR.new.XXXXXX')
  tar -x -C \"\$tmp\"
  if [ -f '$DIR/.env' ]; then cp -p '$DIR/.env' \"\$tmp/.env\"; fi
  rm -rf '$DIR.old'
  if [ -d '$DIR' ]; then mv '$DIR' '$DIR.old'; fi
  mv \"\$tmp\" '$DIR'
  rm -rf '$DIR.old'"

if [ "$SEND_ENV" = 1 ]; then
  echo ".env gönderiliyor (CRLF temizlenerek, sadece root okuyabilir)"
  tr -d '\r' < .env | "${SSH[@]}" "umask 077 && cat > '$DIR/.env'"
fi

"${SSH[@]}" "set -e; cd '$DIR'
  test -f .env || { echo '.env yok: scripts/deploy.sh --env ile gönder' >&2; exit 1; }
  docker compose build --pull
  if [ '$START' = 1 ]; then docker compose up -d && docker compose ps; else echo 'Derlendi; başlatılmadı (--no-start).'; fi"
