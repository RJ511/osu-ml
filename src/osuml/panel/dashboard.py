"""Painel único (`osuml panel`, porta 8765): pedidos à API + categorização de jogadores e mapas.

Uma só página com separadores (Recolha · Categorização · Explorar · Lado a lado); cada separador é a página
em direto da sua secção (`/collect`, `/categorize`). Nada arranca sozinho: os pedidos à API só saem
com "Iniciar" na Recolha; a Categorização só lê a BD e os `.osu` locais (0 pedidos).
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from typing import Any

from .server import PAGE as COLLECT_PAGE
from .server import actions as collect_actions
from .server import make_multi_server

TITLES = {"collect": "Recolha · pedidos à API", "categorize": "Categorização · em direto",
          "explore": "Explorar · jogadores e mapas"}
BOTH = ["collect", "categorize"]

SHELL = r"""<!doctype html><html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel osu!ml</title><style>
:root{--bg:#f5f2ea;--surface:#fff;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--border:#383642;--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;color-scheme:dark}}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;display:flex;flex-direction:column}
.top{display:flex;gap:8px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap}
.top b{margin-right:8px}.top button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer}
.top button.on{background:var(--accent);border-color:var(--accent);color:#fff}.top span{color:var(--dim);font-size:12px;margin-left:auto}
#frames{flex:1;display:grid;min-height:0}iframe{width:100%;height:100%;border:0;background:var(--bg)}
#frames.one{grid-template-columns:1fr}#frames.two{grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}
@media(max-width:1100px){#frames.two{grid-template-columns:1fr;grid-template-rows:1fr 1fr}}
</style></head><body>
<div class="top"><b>Painel osu!ml</b><span id="tabs"></span><span>só localhost · nenhum pedido à API sai sem carregares em Iniciar na Recolha</span></div>
<div id="frames"></div>
<script>
const NAMES=__NAMES__,TITLES=__TITLES__,BOTH=__BOTH__.filter(n=>NAMES.includes(n));let mode=null;
const frames=document.getElementById("frames"),tabs=document.getElementById("tabs");
const iframes={};NAMES.forEach(n=>{const f=document.createElement("iframe");f.src="/"+n;f.title=TITLES[n];iframes[n]=f;frames.appendChild(f)});
function set(m){mode=m;try{localStorage.setItem("panelMode",m)}catch(e){}
 NAMES.forEach(n=>{iframes[n].style.display=(m==="both"?BOTH.includes(n):m===n)?"block":"none"});
 frames.className=m==="both"?"two":"one";
 [...tabs.children].forEach(b=>b.classList.toggle("on",b.dataset.m===m))}
const opts=NAMES.map(n=>[n,TITLES[n]]).concat(BOTH.length>1?[["both","Lado a lado"]]:[]);
opts.forEach(([m,t])=>{const b=document.createElement("button");b.textContent=t;b.dataset.m=m;b.onclick=()=>set(m);tabs.appendChild(b)});
let saved=null;try{saved=localStorage.getItem("panelMode")}catch(e){}
set(opts.some(o=>o[0]===saved)?saved:(BOTH.length>1?"both":NAMES[0]));
</script></body></html>"""


def make_dashboard(collect_ctrl: Any, categorize_ctrl: Any | None, port: int, checker_factory: Any | None = None) -> tuple[ThreadingHTTPServer, str]:
    from ..categorize.server import PAGE as CATEGORIZE_PAGE
    from ..categorize.server import actions as categorize_actions

    sections: dict[str, tuple] = {"collect": (COLLECT_PAGE, collect_ctrl.state, collect_actions(collect_ctrl))}
    if categorize_ctrl is not None:
        sections["categorize"] = (CATEGORIZE_PAGE, categorize_ctrl.state, categorize_actions(categorize_ctrl))
        from ..explore import Explorer
        from ..explore.server import PAGE as EXPLORE_PAGE
        from ..explore.server import actions as explore_actions

        explorer = Explorer(categorize_ctrl.store)
        checker = checker_factory(explorer) if checker_factory is not None else None
        sections["explore"] = (EXPLORE_PAGE, lambda: {"ok": True}, explore_actions(explorer, checker))

    def shell(names: list[str]) -> str:
        import json

        return SHELL.replace("__NAMES__", json.dumps(names)).replace("__TITLES__", json.dumps(TITLES)).replace("__BOTH__", json.dumps(BOTH))

    return make_multi_server(shell, sections, port)
