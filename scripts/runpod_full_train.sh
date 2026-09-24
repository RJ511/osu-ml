#!/usr/bin/env bash
# Prepara o ambiente do pod e lança `pod_pipeline.py` em segundo plano (sobrevive ao fim da ligação SSH).
# Pasta de trabalho $WORK (omissão /root/work) com: osuml_src.zip, pod_pipeline.py, inputs/ (Parquet tratados no PC).
# Não usa a osu!API nem recebe credenciais.
set -euo pipefail
export WORK="${WORK:-/root/work}"
cd "$WORK"
mkdir -p progress logs
command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip curl; }
command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
rm -rf osuml_src && unzip -q -o osuml_src.zip -d osuml_src
[ -d .venv ] || uv venv --python 3.13 .venv
# shellcheck disable=SC1091
. .venv/bin/activate
uv pip install -q "./osuml_src[parquet,ml,difficulty]"
echo "ambiente pronto ($(date)); a lançar o pipeline"
setsid nohup python pod_pipeline.py "$@" > logs/pipeline.out 2>&1 < /dev/null &
echo "pipeline lançado (pid $!)"
