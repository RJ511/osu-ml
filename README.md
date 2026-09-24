# osu! ML Skill & Recommendation System — Fase 0 (collector)

Collector incremental e auditável dos scores de um jogador, usando apenas a
osu!API v2 oficial e seguindo as suas regras de utilização.

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[parquet,dev]"
cp .env.example .env               # preencher OSU_CLIENT_ID / OSU_CLIENT_SECRET
```

A aplicação OAuth cria-se em <https://osu.ppy.sh/home/account/edit#new-oauth-application>.
O Callback URL pode ficar vazio: o collector usa **Client Credentials** (scope `public`),
que chega para ler dados públicos.

## Utilização

```bash
python -m osuml collect --user "PXD Vieira"     # recolha incremental
python -m osuml status  --user "PXD Vieira"     # relatório local (0 pedidos à API)
python -m osuml export  --user "PXD Vieira" --version v0.1
```

Opções úteis de `collect`: `--force-snapshot` (refaz best/firsts/pinned),
`--mode osu|taiko|fruits|mania`, `--max-start-delay 1800` (jitter para cron), `-v`.

## Fontes e o que cada uma dá

| Fonte | Endpoint | Conteúdo | Frequência |
|---|---|---|---|
| `best` | `/users/{id}/scores/best` | top plays (só sucessos) | 1x por TTL (7 dias) |
| `firsts` | `/users/{id}/scores/firsts` | #1 globais | 1x por TTL |
| `pinned` | `/users/{id}/scores/pinned` | scores fixados | 1x por TTL |
| `recent` | `/users/{id}/scores/recent?include_fails=1` | **passes e fails** das últimas 24h, máx. 100 | cada execução |

Cada score é guardado uma vez (`scores.score_id` é PRIMARY KEY). A tabela
`score_observations` regista que fonte viu que score em que pedido, para medir
viés de seleção. `scores.first_source` indica onde apareceu primeiro.

## Regras respeitadas

| Regra | Implementação |
|---|---|
| ≤ 60 pedidos/min | intervalo mínimo de 1,1 s entre pedidos; collector sequencial |
| Exponential backoff | retry em 429/5xx/erros de rede, com jitter; respeita `Retry-After` |
| Não repetir pedidos | TTL do utilizador (24h) e do snapshot (7 dias) |
| Cache e reutilização | respostas raw guardadas; metadata de beatmaps embutida reaproveitada |
| Polling irregular, não "a cada minuto" | `--max-start-delay` + agendamento de poucas horas |
| Sem harvesting | só o utilizador pedido; dados em massa → data.ppy.sh |
| Token secreto | só em memória; nunca em logs, disco ou git; logs do httpx silenciados |

Uma execução típica faz **2 pedidos** (token + recent). Quando os TTL expiram,
junta-se o lookup do utilizador e as páginas de best/firsts/pinned (≈ 5–8 pedidos).

## Agendamento recomendado

O `recent` só cobre 24h e devolve no máximo 100 scores. Para não perder fails,
corre o collector **3–4 vezes por dia**, com jitter.

Linux/macOS (cron, a cada 6h, com até 30 min de jitter):

```cron
0 */6 * * * cd /caminho/osu-ml && .venv/bin/python -m osuml collect --user "PXD Vieira" --max-start-delay 1800 >> data/collect.log 2>&1
```

Windows: Agendador de Tarefas → acionador diário repetido a cada 6 horas →
programa `.venv\Scripts\python.exe`, argumentos
`-m osuml collect --user "PXD Vieira" --max-start-delay 1800`, iniciar em `C:\caminho\osu-ml`.

## Lacunas de cobertura

O collector não finge ter dados que não tem. Regista em `coverage_gaps`:

- `recent`: intervalo entre execuções > 24h, ou resposta com 100 scores que não
  chega à execução anterior (sinal para aumentar a frequência).

`status` mostra estas lacunas, a proporção de fails e a contagem por fonte.

## Estrutura de dados

```text
data/
  osuml.db                       SQLite (ou PostgreSQL via OSUML_DATABASE_URL)
  raw/osu/{data}/{sha256}.json.gz   respostas originais, nunca apagadas
  processed/{versão}/scores_{id}.parquet + manifest_{id}.json
```

Tabelas: `runs`, `api_requests`, `users`, `scores`, `score_observations`,
`beatmaps`, `beatmapsets`, `collector_state`, `coverage_gaps`.

## Testes

```bash
pytest
```

Os testes simulam a osu!API (sem rede) e cobrem deduplicação entre
fontes, incrementalidade, lacunas, retries, renovação de token, revisões de
score (recálculo de pp) e ausência de segredos nos logs.

## Limitações conhecidas

- Sem backfill completo do passado: a API não o permite. Antes do primeiro
  `collect` só existem os scores de best/firsts/pinned (sucessos). A partir daí,
  o histórico (incluindo fails) é completo, desde que o agendamento não deixe
  passar mais de 24h nem mais de 100 plays entre execuções.
- `beatmaps` só tem a metadata embutida nas respostas da API. A aquisição de
  beatmaps em falta é a fase seguinte.
- `nomod_star_rating` no export é o SR **sem mods**; SR com mods virá do
  endpoint de atributos ou de cálculo local a partir dos `.osu`.

## Fase seguinte: ficheiros de beatmap (.osu)

Não é preciso ter o osu! instalado. Cada ficheiro é identificado pelo MD5, que
tem de coincidir com o `checksum` que a API devolveu para esse mapa.

1. **Dump oficial (recomendado, 0 pedidos à API).** Em <https://data.ppy.sh>
   descarrega o arquivo mais recente com os ficheiros `.osu` (ranked/loved) e
   corre:

   ```bash
   python -m osuml maps import --path caminho/para/*_osu_files.tar.bz2 --user "PXD Vieira"
   ```

   O arquivo é lido em streaming e só os mapas que jogaste são guardados em
   `data/raw/osu_files/{md5}.osu`. Também aceita uma pasta (ex.: cópia da pasta
   Songs de outra máquina) ou `.zip`/`.osz`.

2. **Fallback opcional** para mapas fora do dump (unranked/graveyard):

   ```bash
   python -m osuml maps status --user "PXD Vieira"     # vê missing_by_status
   python -m osuml maps fetch  --user "PXD Vieira"     # 1 pedido por mapa, 1,1 s entre pedidos
   ```

   Usa `https://osu.ppy.sh/osu/{id}`, uma rota do site que **não** faz parte da
   osu!API v2 documentada. Usa-a só para os poucos mapas em falta.

3. **Parsing e export:**

   ```bash
   python -m osuml maps export --user "PXD Vieira" --version v0.2
   ```

   Gera `beatmaps_<id>.parquet` (1 linha por mapa: AR/OD/CS/HP, contagens,
   duração) e `hitobjects_<id>.parquet` (1 linha por objeto: tempo, x, y, tipo,
   dados de slider, fim do slider, beatLength e SV ativos).
