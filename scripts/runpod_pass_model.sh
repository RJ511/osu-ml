#!/usr/bin/env bash
# Treino + avaliação do modelo P(passar alguma vez | jogador, mapa) num pod RunPod só de CPU.
#
# Pasta de trabalho (ex.: /root/work) com: osuml_src.zip, inputs/ (playcount, dump_scores, map_attributes, api_plays .parquet)
# e este script. Uso: bash runpod_pass_model.sh        (retomável só até ao fim da leitura de dados; o treino recomeça)
# Saída: pass_model_results.tar.gz  (results.json + modelos A e C) e out/v1/progress.json (lido pelo painel principal por SSH).
# Não faz pedidos à osu!API nem precisa de credenciais.
set -euo pipefail
cd "$(dirname "$0")"

for f in osuml_src.zip inputs/map_attributes.parquet inputs/api_plays.parquet; do
  [ -e "$f" ] || { echo "Falta $f nesta pasta" >&2; exit 1; }
done
command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; }
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

rm -rf osuml_src && unzip -q -o osuml_src.zip -d osuml_src
[ -d .venv ] || uv venv --python 3.13 .venv
# shellcheck disable=SC1091
. .venv/bin/activate
uv pip install "./osuml_src[parquet,ml]"

THREADS="${THREADS:-$(nproc)}"
echo "A treinar com $THREADS threads ($(date))"
python -m osuml analyze pass-model --inputs ./inputs --out-dir ./out --version v1 --progress ./out/v1/progress.json \
  --threads "$THREADS" --seeds 42,43,44 --rounds 600 \
  --players "PXD Vieira=13745526" "gaaGOD=23994179"
tar -czf pass_model_results.tar.gz -C out v1
ls -la pass_model_results.tar.gz
echo "Pronto ($(date))."
