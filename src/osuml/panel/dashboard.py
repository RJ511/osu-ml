"""Painel único (`osuml panel`, porta 8765): pedidos à API + categorização de jogadores e mapas.

Uma só página com separadores (Recolha · Categorização · Explorar · Tarefas · Lado a lado); cada separador é a página
em direto da sua secção (`/collect`, `/categorize`). Nada arranca sozinho: os pedidos à API só saem
com "Iniciar" na Recolha; a Categorização só lê a BD e os `.osu` locais (0 pedidos).
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .server import PAGE as COLLECT_PAGE
from .server import actions as collect_actions
from .server import make_multi_server

TITLES = {"tags": "Tags · estado", "evolution": "Evolução", "collect": "Recolha · pedidos à API", "categorize": "Categorização · em direto",
          "explore": "Explorar · jogadores e mapas", "tasks": "Tarefas · progresso"}
BOTH = ["collect", "categorize"]

SHELL = r"""<!doctype html><html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel osu!ml</title><style>
:root{--bg:#f5f2ea;--surface:#fff;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--border:#383642;--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;color-scheme:dark}}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;display:flex;flex-direction:column}
.top{display:flex;gap:8px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap}
.top{overflow-x:auto;flex-wrap:nowrap}.top #tabs{display:flex;gap:8px}.top button{white-space:nowrap}.top b{margin-right:8px;white-space:nowrap}.top button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer}
.top button.on{background:var(--accent);border-color:var(--accent);color:#fff}.top span.note{color:var(--dim);font-size:12px;margin-left:auto;white-space:nowrap}@media(max-width:700px){.top span.note{display:none}}
#strip{display:none;gap:10px;padding:6px 16px;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap;align-items:center}
#strip .t{display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;min-width:230px}#strip .t span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
#strip .b{flex:1;min-width:90px;height:8px;border-radius:99px;background:var(--border);overflow:hidden}#strip .b i{display:block;height:100%;background:var(--accent);transition:width .6s}
#strip small{color:var(--dim);font-family:ui-monospace,monospace}
#frames{flex:1;display:grid;min-height:0}iframe{width:100%;height:100%;border:0;background:var(--bg)}
#frames.one{grid-template-columns:1fr}#frames.two{grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}
@media(max-width:1100px){#frames.two{grid-template-columns:1fr;grid-template-rows:1fr 1fr}}
</style></head><body>
<div class="top"><b>Painel osu!ml</b><span id="tabs"></span><span class="note">nenhum pedido à API sai sem carregares em Iniciar na Recolha</span></div>
<div id="strip"></div>
<div id="frames"></div>
<script>
const NAMES=__NAMES__,TITLES=__TITLES__,BOTH=__BOTH__.filter(n=>NAMES.includes(n));let mode=null;
const frames=document.getElementById("frames"),tabs=document.getElementById("tabs");
const iframes={};NAMES.forEach(n=>{const f=document.createElement("iframe");f.src="/"+n;f.title=TITLES[n];iframes[n]=f;frames.appendChild(f)});
function set(m){mode=m;try{localStorage.setItem("panelMode",m)}catch(e){}
 NAMES.forEach(n=>{iframes[n].style.display=(m==="both"?BOTH.includes(n):m===n)?"block":"none"});
 frames.className=m==="both"?"two":"one";
 [...tabs.children].forEach(b=>b.classList.toggle("on",b.dataset.m===m))}
const NARROW=matchMedia("(max-width:900px)").matches;  // telemóvel: sem "Lado a lado"
const opts=NAMES.map(n=>[n,TITLES[n]]).concat(BOTH.length>1&&!NARROW?[["both","Lado a lado"]]:[]);
opts.forEach(([m,t])=>{const b=document.createElement("button");b.textContent=t;b.dataset.m=m;b.onclick=()=>set(m);tabs.appendChild(b)});
let saved=null;try{saved=localStorage.getItem("panelMode")}catch(e){}
set(opts.some(o=>o[0]===saved)?saved:NAMES[0]);
// faixa com as tarefas em curso, visível em qualquer separador (clicar abre "Tarefas")
async function strip(){if(!NAMES.includes("tasks"))return;const st=document.getElementById("strip");
 try{const s=await(await fetch("/tasks/api/state")).json();const run=s.tasks.filter(t=>t.status==="running");
  st.style.display=run.length?"flex":"none";
  st.replaceChildren(...run.map(t=>{const d=document.createElement("div");d.className="t";d.title=t.label;d.onclick=()=>set("tasks");
   const l=document.createElement("span");l.textContent=t.label;const b=document.createElement("div");b.className="b";const i=document.createElement("i");i.style.width=t.pct+"%";b.appendChild(i);
   const p=document.createElement("small");p.textContent=t.pct.toFixed(0)+"%";d.append(l,b,p);return d}))}catch(e){}}
strip();setInterval(strip,2500);
</script></body></html>"""


def make_dashboard(collect_ctrl: Any, categorize_ctrl: Any | None, port: int, checker_factory: Any | None = None,
                   jobs: Any | None = None, recommender: Any | None = None,
                   goal_path: Any | None = None, tags_monitor: Any | None = None) -> tuple[ThreadingHTTPServer, str]:
    from ..categorize.server import PAGE as CATEGORIZE_PAGE
    from ..categorize.server import actions as categorize_actions

    sections: dict[str, tuple] = {"collect": (COLLECT_PAGE, collect_ctrl.state, collect_actions(collect_ctrl))}
    if categorize_ctrl is not None:
        sections["categorize"] = (CATEGORIZE_PAGE, categorize_ctrl.state, categorize_actions(categorize_ctrl))
        from ..explore import Explorer
        from ..explore.server import PAGE as EXPLORE_PAGE
        from ..explore.server import actions as explore_actions

        attrs = None
        if recommender is not None:
            cand = recommender.index_dir.parents[1] / "catalog" / "v2" / "map_attributes.parquet"
            attrs = cand if cand.exists() else None
        explorer = Explorer(categorize_ctrl.store, catalog=recommender.catalog if recommender is not None else None, attributes=attrs)
        checker = checker_factory(explorer) if checker_factory is not None else None
        sections["explore"] = (EXPLORE_PAGE, lambda: {"ok": True}, explore_actions(explorer, checker, recommender))
        if goal_path is not None:
            from ..evolution import Evolution
            from ..evolution.server import PAGE as EVOLUTION_PAGE
            from ..evolution.server import actions as evolution_actions

            from ..evolution.weekly import WeeklyPlanner

            from ..evolution.live import LiveCalibrator
            from ..evolution.notify import Notifier
            from ..mirror.client import StrainsPrefetcher

            import json as _json

            try:
                uid = int(_json.loads(Path(goal_path).read_text(encoding="utf-8")).get("user_id") or 0)
            except (OSError, ValueError):
                uid = 0
            live = LiveCalibrator(categorize_ctrl.store, recommender, uid) if recommender is not None and uid else None
            evo = Evolution(explorer, goal_path, weekly=WeeklyPlanner(recommender, Path(goal_path).parent),
                            notifier=Notifier(Path(goal_path).parent / "notifications.json"), recommender=recommender, checker=checker, live=live,
                            strains=StrainsPrefetcher(categorize_ctrl.store, Path(categorize_ctrl.store.raw.raw_dir) / "hinai_strains", Path(goal_path).parent / "mirror_usage.json"))
            if live is not None:
                live.start()
            # primeiro separador (e o de arranque): é a vista para o dia a dia, também no telemóvel
            sections = {"evolution": (EVOLUTION_PAGE, evo.state, evolution_actions(evo)), **sections}

    if tags_monitor is not None:
        from ..tags.server import PAGE as TAGS_PAGE
        from ..tags.server import actions as tags_actions

        sections["tags"] = (TAGS_PAGE, tags_monitor.state, tags_actions(tags_monitor))

    if jobs is not None:
        from ..jobs.server import PAGE as TASKS_PAGE
        from ..jobs.server import actions as tasks_actions

        sections["tasks"] = (TASKS_PAGE, jobs.state, tasks_actions(jobs))

    def shell(names: list[str]) -> str:
        import json

        return SHELL.replace("__NAMES__", json.dumps(names)).replace("__TITLES__", json.dumps(TITLES)).replace("__BOTH__", json.dumps(BOTH))

    return make_multi_server(shell, sections, port)
