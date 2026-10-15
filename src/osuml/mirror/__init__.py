"""Usos autorizados do mirror hinai que não são as tags: `.osu` em falta, meta semanal do top 100 PT e dificuldade ao longo do mapa.

Todos passam pelo mesmo cliente (`client.MirrorClient`): User-Agent do projeto, ≥ 1 s entre pedidos, semáforo do guard (pára só com dor alta),
Retry-After respeitado, 3 falhas seguidas param tudo e um teto diário por finalidade (`usage.json`)."""
