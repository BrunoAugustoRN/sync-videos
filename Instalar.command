#!/bin/bash
# Dê dois cliques neste arquivo no Finder pra instalar tudo que o Sync
# Videos precisa nesta máquina (ffmpeg, Python e as bibliotecas). Só
# precisa rodar uma vez em cada computador novo.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=== Instalando o Sync Videos ==="
echo

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew não encontrado, instalando (pode pedir sua senha)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [ -d /opt/homebrew/bin ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -d /usr/local/bin ]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

echo
echo "Instalando ffmpeg e Python (pode demorar alguns minutos na primeira vez)..."
brew install ffmpeg python@3.14

echo
echo "Preparando o ambiente do programa..."
PYBIN="$(brew --prefix python@3.14)/bin/python3.14"
if [ ! -x "$PYBIN" ]; then
    PYBIN="python3"
fi
rm -rf .venv
"$PYBIN" -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install numpy scipy --quiet

echo
echo "=== Tudo pronto! ==="
echo "Pode fechar esta janela e abrir o SyncVideosGUI.app normalmente."
echo
open "$DIR/SyncVideosGUI.app"
read -p "Pressione Enter para fechar..."
