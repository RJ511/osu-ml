"""Aplicação local do recomendador (sem API do osu!, sem credenciais): escreve-se o nome de um jogador que já está na base de dados,
escolhem-se as skills e recebem-se mapas; cada sugestão pode ser marcada "Serve / Não serve" e o feedback fica num ficheiro de texto (TSV).

`osuml recommend serve [--pack PASTA]` -> http://127.0.0.1:8770 (só localhost; as ações exigem o token da página).
"""

from __future__ import annotations

from typing import Any

PAGE = """<!doctype html><html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>osu!ml — recomendar mapas</title>
<style>
:root{--bg:#14161a;--fg:#e6e8ec;--dim:#9aa3b2;--card:#1d2026;--line:#2b303a;--acc:#ff66aa;--ok:#5cd08a;--bad:#ef6b6b}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#1b1e24;--dim:#5b6472;--card:#fff;--line:#dfe3ea;--acc:#c2185b;--ok:#1f8a4c;--bad:#c62828}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim);font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
input,button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:7px;padding:6px 10px}
input{min-width:240px;background:var(--bg)}button{cursor:pointer}button.on{background:var(--acc);border-color:var(--acc);color:#fff}
button.go{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button:disabled{opacity:.5;cursor:default}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:600;white-space:nowrap}td.num{text-align:right;white-space:nowrap}td.why{color:var(--dim);font-size:12px;min-width:260px}
a{color:var(--acc)}.tag{border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:12px}.err{color:var(--bad)}.ok{color:var(--ok)}
.tblwrap{overflow-x:auto}
</style></head><body><main>
<h1>Recomendar mapas</h1>
<div class="sub">Escreve um jogador que já esteja na base de dados, escolhe a(s) skill(s) que queres melhorar e recebe mapas que te desafiem nelas, alcançáveis e do estilo de jogadores parecidos. Não faz nenhum pedido à API do osu!.</div>
<div class="card">
 <div class="row"><input id="player" list="players" placeholder="Nome do jogador (ex.: PXD Vieira)" autocomplete="off"><datalist id="players"></datalist><span class="sub" id="pinfo"></span></div>
 <div class="row" id="skills"></div>
 <div class="row"><button class="go" id="go">Recomendar</button><span class="sub" id="msg"></span></div>
</div>
<div id="out"></div>
<div class="sub" id="fbfile"></div>
</main>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(body)});return r.json()}
const SK=[["aim","Aim"],["speed","Speed"],["stamina","Stamina"],["reading","Reading"]],KIND={novo:"novo",rejogar:"rejogar",tentar_de_novo:"tentar de novo"};
const sel=new Set();
SK.forEach(([k,l])=>{const b=el("button","",l);b.onclick=()=>{if(sel.has(k)){sel.delete(k);b.classList.remove("on")}else{sel.add(k);b.classList.add("on")}};$("skills").appendChild(b)});
let players=[];
fetch("/api/state").then(r=>r.json()).then(s=>{players=s.players||[];const dl=$("players");players.forEach(p=>{const o=document.createElement("option");o.value=p.username;dl.appendChild(o)});
 $("pinfo").textContent=players.length?players.length+" jogador(es) disponíveis: "+players.map(p=>p.username).join(", "):"sem jogadores na base de dados";$("fbfile").textContent=(s.feedback_file?"O feedback fica guardado em: "+s.feedback_file+" · ":"")+(s.model&&s.model.fingerprint?"Modelo "+s.model.fingerprint+(s.model.training&&s.model.training.players?" (treinado com "+s.model.training.players+" jogadores)":""):"sem modelo carregado")});
$("go").onclick=async()=>{
 const name=$("player").value.trim();if(!name){$("msg").textContent="escreve o nome de um jogador";return}
 if(!sel.size){$("msg").textContent="escolhe pelo menos uma skill";return}
 $("go").disabled=true;$("msg").textContent="a calcular…";$("out").replaceChildren();
 let r;try{r=await post("/api/recommend",{player:name,skills:[...sel]})}catch(e){r={error:"sem ligação ao servidor"}}
 $("go").disabled=false;if(r.error){$("msg").textContent=r.error;$("msg").className="sub err";return}
 $("msg").className="sub";$("msg").textContent=r.items.length+" sugestões para "+r.player.username+" · candidatos: "+r.counts.novo+" novos, "+r.counts.rejogar+" a repetir, "+r.counts.tentar_de_novo+" a tentar de novo";
 const lv=r.player.levels,card=el("div","card");
 card.appendChild(el("div","sub","O teu nível (P90 dos melhores passes; 50 = mapa mediano): "+["aim","speed","stamina","reading"].map(a=>a+" "+lv[a].toFixed(0)).join(" · ")));
 const wrap=el("div","tblwrap"),tb=el("table"),hd=el("tr");
 ["#","Mapa","Tipo","★","Desafio","≥88 %","Acc prov.","Acc atual","pp est.","Estilo","Porquê","Feedback"].forEach(h=>hd.appendChild(el("th","",h)));tb.appendChild(hd);
 r.items.forEach((x,i)=>{const tr=el("tr");tr.appendChild(el("td","num",String(i+1)));
  const tm=el("td"),a=el("a","",x.label);a.href=x.url;a.target="_blank";a.rel="noopener noreferrer";tm.appendChild(a);tm.appendChild(el("div","sub","ID do mapa: "+x.beatmap_id+(x.beatmapset_id?" · set "+x.beatmapset_id:"")));tr.appendChild(tm);
  tr.appendChild(el("td","",KIND[x.kind]||x.kind));tr.appendChild(el("td","num",x.stars.toFixed(2)));
  tr.appendChild(el("td","num",r.skills.map(s=>s+" "+(x.delta[s]>=0?"+":"")+x.delta[s].toFixed(1)).join(" · ")));
  tr.appendChild(el("td","num",Math.round(x.p88*100)+" %"));tr.appendChild(el("td","num",(x.acc_pred*100).toFixed(1)+" %"));
  tr.appendChild(el("td","num",x.acc_cur==null?"—":(x.acc_cur*100).toFixed(1)+" %"));tr.appendChild(el("td","num",x.pp_gain_pct==null?"—":"+"+Math.round(x.pp_gain_pct)+" %"));
  tr.appendChild(el("td","num",x.style_pct.toFixed(0)));tr.appendChild(el("td","why",x.why));
  const fb=el("td"),y=el("button","","Serve"),n=el("button","","Não serve");
  const send=async v=>{const res=await post("/api/feedback",{player:r.player.username,beatmap_id:x.beatmap_id,verdict:v,skills:r.skills,kind:x.kind,score:x.score});
   fb.replaceChildren(el("span",res.ok?"ok":"err",res.ok?(v==="serve"?"guardado: serve":"guardado: não serve"):(res.error||"erro")))};
  y.onclick=()=>send("serve");n.onclick=()=>send("nao_serve");fb.append(y,n);tr.appendChild(fb);tb.appendChild(tr)});
 wrap.appendChild(tb);card.appendChild(wrap);(r.notes||[]).forEach(t=>card.appendChild(el("div","sub","· "+t)));$("out").appendChild(card)};
</script></body></html>"""


def build_app(recommender: Any, port: int):
    """(servidor, token). `recommender` é um `Recommender` já configurado (com `feedback_file` para a cópia em texto)."""
    from ..panel.server import make_generic_server

    def state() -> dict[str, Any]:
        return {"players": recommender.players(), "feedback_file": str(recommender.feedback_file) if recommender.feedback_file else None,
                "model": recommender.model_info()}

    def recommend(body: dict) -> dict:
        found = recommender.find_player(body.get("player"))
        if found is None:
            names = ", ".join(p["username"] for p in recommender.players()) or "nenhum"
            return {"error": f"jogador não encontrado na base de dados (disponíveis: {names})"}
        skills = body.get("skills") if isinstance(body.get("skills"), list) else []
        res = recommender.recommend(found[0], [str(s) for s in skills], n=20)
        if "error" not in res:
            res["player"] = {**res["player"], "username": found[1]}
        return res

    def feedback(body: dict) -> dict:
        found = recommender.find_player(body.get("player"))
        try:
            bid = int(body.get("beatmap_id"))
        except (TypeError, ValueError):
            return {"error": "mapa inválido"}
        if found is None:
            return {"error": "jogador não encontrado"}
        skills = body.get("skills") if isinstance(body.get("skills"), list) else []
        try:
            score = float(body["score"]) if body.get("score") is not None else None
        except (TypeError, ValueError):
            score = None
        return recommender.feedback(found[0], bid, str(body.get("verdict") or ""), [str(s) for s in skills], str(body.get("kind") or ""), score,
                                    str(body.get("note") or "")[:500])

    return make_generic_server(PAGE, state, {"/api/recommend": recommend, "/api/feedback": feedback}, port)
