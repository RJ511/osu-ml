#!/usr/bin/env bash
# Catálogo de atributos de mapas (osuml map-catalog run) num pod RunPod só de CPU.
#
# Pasta de trabalho no pod (ex.: /root/work) com estes 4 ficheiros (vindos de data/runpod/ no teu PC):
#   osuml_src.zip        código do projeto (src/ + pyproject.toml)
#   plan.json            pares (mapa, mods) a calcular
#   osu_subset.tar.gz    só os .osu do plano, extraídos do dump oficial (dados privados: licença do ppy)
#   runpod_catalog.sh    este script
#
# Uso:   bash runpod_catalog.sh            (retomável: se o pod reiniciar, volta a correr e salta o que já está feito)
# Saída: catalog_parts.tar.gz  (as partes calculadas; trazê-lo para o PC e fazer `map-catalog merge`)
#
# Não faz nenhum pedido à osu!API nem precisa de credenciais.
set -euo pipefail
cd "$(dirname "$0")"

for f in osuml_src.zip plan.json osu_subset.tar.gz; do
  [ -f "$f" ] || { echo "Falta $f nesta pasta" >&2; exit 1; }
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
uv pip install "./osuml_src[parquet,difficulty]"

WORKERS="${WORKERS:-$(nproc)}"
echo "A calcular com $WORKERS processos ($(date))"
python -m osuml map-catalog run --source osu_subset.tar.gz --plan plan.json --version v1 \
  --out-dir ./out --workers "$WORKERS"

tar -czf catalog_parts.tar.gz -C out v1/parts
ls -la catalog_parts.tar.gz
echo "Pronto ($(date)). Descarrega catalog_parts.tar.gz e desliga/apaga o pod."
