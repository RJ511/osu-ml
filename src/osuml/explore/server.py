"""Secção "Explorar" do painel (`/explore`): pesquisar jogadores e mapas, ver atributos e plays.
Só lê a BD local (0 pedidos à API). As ações são POST (token da página), como nas outras secções."""

from __future__ import annotations

from typing import Any, Callable

from .core import Explorer

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Explorar</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;
--good:#2f8f57;--warn:#b9791f;--bad:#c14545;--aim:#6f5fc9;--speed:#1f8f86;--stamina:#c9622f;--reading:#2f74b8;--stars:#8a7a2c;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;
--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;--aim:#a597ee;--speed:#4fc2b7;
--stamina:#f0925e;--reading:#5fa4ea;--stars:#d3c25b;color-scheme:dark}}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;display:flex;flex-direction:column}
.top{display:flex;gap:8px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--surface)}
.top h1{font-size:18px;margin:0 12px 0 0}
button,input,select{font:inherit;padding:7px 11px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}
button{cursor:pointer}button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.main{flex:1;min-height:0;display:grid;grid-template-columns:minmax(280px,380px) 1fr;gap:12px;padding:12px 16px}
@media(max-width:900px){.main{grid-template-columns:1fr;grid-template-rows:minmax(160px,40%) 1fr}}
.pane{background:var(--surface);border:1px solid var(--border);border-radius:12px;display:flex;flex-direction:column;min-height:0;min-width:0}
.pane .hd{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center}.pane .hd input{flex:1;min-width:0}
.list{overflow:auto;flex:1}.detail{overflow:auto;padding:14px 16px;flex:1}
.row{padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer}.row:hover{background:var(--surface2)}.row.sel{background:color-mix(in srgb,var(--accent) 14%,transparent)}
.row .n{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sub{color:var(--dim);font-size:12.5px}.dim{color:var(--dim)}
.mono{font-family:ui-monospace,monospace}.big{font:700 22px system-ui;margin:0}
.g{font:700 11px ui-monospace,monospace;padding:1px 6px;border-radius:5px;margin-right:3px;background:var(--surface2)}
.g.hi{background:#e6b422;color:#221f2b}.g.a{background:color-mix(in srgb,var(--good) 30%,transparent)}.g.b{background:color-mix(in srgb,var(--reading) 28%,transparent)}
.axes{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:12px 0}
.ax{border:1px solid var(--border);border-radius:10px;padding:8px 10px;border-top:3px solid var(--c)}.ax .lbl{font-size:11px;text-transform:uppercase;color:var(--dim)}
.ax b{font:700 20px ui-monospace,monospace}.trk{height:5px;border-radius:9px;background:var(--surface2);margin-top:5px;overflow:hidden}.trk i{display:block;height:100%;background:var(--c)}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.tools input{min-width:200px}
table{border-collapse:collapse;width:100%;font-size:12.5px}th{position:sticky;top:-14px;background:var(--surface2);text-align:left;font-size:11px;text-transform:uppercase;color:var(--dim);padding:6px 8px;cursor:pointer;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap}td.wrap{white-space:normal;min-width:220px}
a.lk{color:var(--accent);cursor:pointer;text-decoration:none}a.lk:hover{text-decoration:underline}
.pass{color:var(--good)}.fail{color:var(--bad)}.chip{display:inline-block;font:600 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;background:var(--surface2);margin-right:4px}
.empty{padding:24px;color:var(--dim);text-align:center}h2{font-size:12px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.tblwrap{overflow:auto}
.card2{border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin:12px 0;display:flex;flex-direction:column;gap:4px}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:not-allowed}
</style></head><body>
<div class="top"><h1>Explorar</h1><button id="tabP">Jogadores</button><button id="tabM">Mapas</button>
<span class="sub" id="info" style="margin-left:auto">só lê a base de dados local · 0 pedidos à API</span></div>
<div class="main"><div class="pane"><div class="hd"><input id="q" type="search" placeholder="Pesquisar…" autocomplete="off"></div>
<div class="sub" id="count" style="padding:6px 12px"></div><div class="list" id="list"></div></div>
<div class="pane"><div class="detail" id="detail"><div class="empty">Escolhe um item à esquerda.</div></div></div></div>
<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(b||{})}).then(r=>r.json());
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined&&x!==null)e.textContent=x;return e};
const AX=[["aim","Aim","--aim"],["speed","Speed","--speed"],["stamina","Stamina","--stamina"],["reading","Reading","--reading"],["stars","Estrelas","--stars"]];
const gc=g=>["X","SSS","SS","S+","S"].includes(g)?"hi":g==="A"?"a":(g==="B+"||g==="B")?"b":"";
const n1=v=>v==null?"—":Number(v).toFixed(1),n2=v=>v==null?"—":Number(v).toFixed(2),n0=v=>v==null?"—":Math.round(v).toLocaleString("pt-PT");
const dt=v=>v?String(v).replace("T"," ").slice(0,16):"—";
let mode="p",selP=null,selM=null,timer=null,chkMsg=null;
const loc=iso=>iso?new Date(iso).toLocaleString("pt-PT",{dateStyle:"short",timeStyle:"medium"}):"nunca";
function ago(iso){if(!iso)return "nunca";const s=Math.max(0,(Date.now()-new Date(iso))/1000);
 if(s<60)return "há "+Math.round(s)+" s";if(s<3600)return "há "+Math.round(s/60)+" min";if(s<172800)return "há "+(s/3600).toFixed(1)+" h";return "há "+Math.round(s/86400)+" dias"}

function gradeChip(a){if(!a)return el("span","dim","–");const s=el("span","g "+gc(a.grade),a.grade||"–");s.title=n1(a.score)+" · top "+(a.pct==null?"?":(100-a.pct).toFixed(1))+"%";return s}
function axCell(a){const w=el("span");if(!a){w.appendChild(el("span","dim","–"));return w}w.appendChild(gradeChip(a));w.appendChild(el("span","mono",n1(a.score)));return w}
function link(text,fn){const a=el("a","lk",text);a.onclick=fn;return a}

function sortable(cols,rows,defKey,defDir){
 let key=defKey,dir=defDir||-1;const wrap=el("div","tblwrap"),tb=el("table"),thead=el("thead"),tr=el("tr"),body=el("tbody");
 cols.forEach((c,i)=>{const th=el("th","",c.h);th.onclick=()=>{if(key===i)dir=-dir;else{key=i;dir=c.num===false?1:-1}draw()};tr.appendChild(th)});
 thead.appendChild(tr);tb.append(thead,body);wrap.appendChild(tb);
 function draw(){const c=cols[key];const s=[...rows].sort((a,b)=>{const x=c.sort(a),y=c.sort(b);
   if(x==null&&y==null)return 0;if(x==null)return 1;if(y==null)return -1;return (x<y?-1:x>y?1:0)*dir});
  body.replaceChildren(...s.map(r=>{const t=el("tr");cols.forEach(c2=>{const td=el("td",c2.cls||"");const v=c2.render(r);td.appendChild(typeof v==="string"?document.createTextNode(v):v);t.appendChild(td)});return t}))}
 draw();return wrap}

function setMode(m){mode=m;$("tabP").classList.toggle("on",m==="p");$("tabM").classList.toggle("on",m==="m");
 $("q").placeholder=m==="p"?"Pesquisar jogador (nome ou id)…":"Pesquisar mapa (artista, título, dificuldade, mapper ou id)…";
 $("q").value="";search();
 const sel=m==="p"?selP:selM;if(sel)show(m,sel);else $("detail").replaceChildren(el("div","empty","Escolhe um item à esquerda."))}
$("tabP").onclick=()=>setMode("p");$("tabM").onclick=()=>setMode("m");
$("q").oninput=()=>{clearTimeout(timer);timer=setTimeout(search,200)};

async function search(){
 const q=$("q").value,r=await post(mode==="p"?"/api/players":"/api/maps",{q});const list=$("list");
 $("count").textContent=r.total!=null?`${r.total.toLocaleString("pt-PT")} resultado(s)${r.items.length<r.total?` · a mostrar ${r.items.length}`:""}${mode==="m"&&r.catalog?` · catálogo de ${r.catalog_size.toLocaleString("pt-PT")} mapas (jogados primeiro)`:""}`:"";
 if(!r.items.length){list.replaceChildren(el("div","empty","Sem resultados."));return}
 list.replaceChildren(...r.items.map(it=>{
  const id=mode==="p"?it.user_id:it.beatmap_id,row=el("div","row"+((mode==="p"?selP:selM)===id?" sel":""));row.dataset.id=id;
  if(mode==="p"){row.appendChild(el("div","n",it.username));
   const s=el("div","sub",`${it.pp!=null?n0(it.pp)+"pp":"pp ?"} · rank ${it.global_rank!=null?"#"+n0(it.global_rank):"?"} · ${it.n_scores} plays · verificado ${ago(it.last_check_at)} `);
   AX.slice(0,4).forEach(([k])=>{const g=it.grades[k];const c=el("span","g "+gc(g),g||"–");if(!it.confident)c.style.opacity=".5";s.appendChild(c)});row.appendChild(s)}
  else{row.appendChild(el("div","n",it.label));row.appendChild(el("div","sub",it.catalog_only?`${n2(it.stars)}★ · só no catálogo (sem plays acompanhados)`:`${n2(it.stars)}★ · ${it.n_plays} plays · ${it.n_players} jogador(es)${it.max_pp?" · máx "+n0(it.max_pp)+"pp":""} · ${it.status||""}`))}
  row.onclick=()=>show(mode,id);return row}))}

async function show(m,id){
 if(m!==mode)setMode(m);
 if(m==="p")selP=id;else selM=id;
 [...$("list").children].forEach(r=>r.classList&&r.classList.toggle("sel",r.dataset&&+r.dataset.id===id));
 try{history.replaceState(null,"","#"+m+"/"+id)}catch(e){}
 const d=$("detail");d.replaceChildren(el("div","empty","A carregar…"));
 const r=await post(m==="p"?"/api/player":"/api/map",{id});
 if(r.error){d.replaceChildren(el("div","empty",r.error));return}
 d.replaceChildren(...(m==="p"?renderPlayer(r):renderMap(r)))}

function renderPlayer(r){
 const p=r.player,out=[],h=el("div");h.appendChild(el("p","big",p.username));
 h.appendChild(el("div","sub",`#${p.user_id}${p.country?" · "+p.country:""} · ${p.pp!=null?n0(p.pp)+"pp":"pp ?"} · rank ${p.global_rank!=null?"#"+n0(p.global_rank):"?"} · ${p.n_scores} plays · evidência ${p.n_evidence??"?"} (passadas, acc ≥ 90%)${p.n_missing?" · "+p.n_missing+" sem categoria":""}${p.confident?"":" · rating pouco fiável (<10 plays de evidência)"}`));
 out.push(h);out.push(checkCard(p.user_id,r.check));out.push(recCard(p.user_id));
 const ax=el("div","axes");const rt=p.ratings||{};
 AX.forEach(([k,l,c])=>{const d=el("div","ax");d.style.setProperty("--c","var("+c+")");d.appendChild(el("div","lbl",l+" · rating P90"));
  const sc=rt[k+"_rating"];const b=el("b","",sc!=null?rt[k+"_grade"]+" "+n1(sc):"–");d.appendChild(b);
  d.appendChild(el("div","sub mono","típico "+n1(rt[k+"_typical"])));const t=el("div","trk"),i=el("i");i.style.width=(Math.min(120,Math.max(0,sc||0))/1.2)+"%";t.appendChild(i);d.appendChild(t);ax.appendChild(d)});
 out.push(ax);
 out.push(el("h2","","Plays ("+r.plays.length+")"));
 const tools=el("div","tools"),f=el("input");f.placeholder="Filtrar por mapa ou mods…";
 const rs=el("select");[["","Todas"],["p","Só passadas"],["f","Só fails"]].forEach(([v,t])=>{const o=el("option","",t);o.value=v;rs.appendChild(o)});
 tools.append(f,rs);out.push(tools);const host=el("div");out.push(host);
 const A=(k)=>({h:k[1],sort:x=>x.axes&&x.axes[k[0]]?x.axes[k[0]].score:null,render:x=>axCell(x.axes&&x.axes[k[0]])});
 const cols=[{h:"Data",sort:x=>x.ended_at,render:x=>dt(x.ended_at),num:false},
  {h:"Mapa",sort:x=>x.map.toLowerCase(),num:false,cls:"wrap",render:x=>link(x.map,()=>show("m",x.beatmap_id))},
  {h:"Mods",sort:x=>x.mods,num:false,render:x=>x.mods||"NM"},{h:"★",sort:x=>x.raw?x.raw.stars:null,render:x=>{if(!x.raw)return "—";const s=el("span","",n2(x.raw.stars));if(x.axes&&x.axes.stars)s.title="nota "+n1(x.axes.stars.score);return s}},
  {h:"Acc",sort:x=>x.accuracy,render:x=>x.accuracy==null?"—":(x.accuracy*100).toFixed(2)+"%"},{h:"pp",sort:x=>x.pp,render:x=>x.pp==null?"—":n0(x.pp)},
  {h:"Resultado",sort:x=>x.passed?1:0,render:x=>{const s=el("span",x.passed?"pass":"fail",x.passed?"pass "+(x.rank||""):"fail");return s}},
  A(AX[0]),A(AX[1]),A(AX[2]),A(AX[3])];
 function draw(){const q=f.value.toLowerCase(),v=rs.value;
  const rows=r.plays.filter(x=>(!q||x.map.toLowerCase().includes(q)||(x.mods||"nm").toLowerCase().includes(q))&&(v===""||(v==="p")===x.passed));
  host.replaceChildren(rows.length?sortable(cols,rows,0,-1):el("div","empty","Sem plays."))}
 f.oninput=draw;rs.onchange=draw;draw();return out}


function checkCard(uid,c){
 const box=el("div","card2"),l1=el("div"),l2=el("div","sub"),l3=el("div","sub"),row=el("div","tools"),btn=el("button","go"),msg=el("span","sub");
 let rem=c.cooldown_remaining_s||0,busy=false;
 const fill=c=>{l1.replaceChildren(el("b","","Última verificação (API): "),document.createTextNode(loc(c.last_check_at)+(c.last_check_at?" · "+ago(c.last_check_at):"")));
  l2.textContent="Próxima verificação agendada: "+(c.tracked_status==="inactive"?"— (jogador inativo, sem pedidos)":c.tracked_status?loc(c.next_poll_at):"— (fora do acompanhamento automático)")+(c.note?" · "+c.note:"");
  l3.textContent="Última categorização: "+loc(c.last_categorized_at)+(c.last_categorized_at?" · "+ago(c.last_categorized_at):"")+(c.scheme?" · "+c.scheme:"")};
 const paint=()=>{btn.disabled=busy||rem>0;btn.textContent=busy?"A verificar…":rem>0?`Verificar agora (aguarda ${Math.ceil(rem)} s)`:"Verificar agora (1 pedido à API)"};
 fill(c);paint();if(chkMsg&&chkMsg.uid===uid)msg.textContent=chkMsg.text;
 const iv=setInterval(()=>{if(!box.isConnected){clearInterval(iv);return}if(rem>0){rem=Math.max(0,rem-0.25);paint()}fill(c)},250);
 btn.onclick=async()=>{if(btn.disabled)return;busy=true;paint();msg.textContent="";
  let r;try{r=await post("/api/check",{id:uid})}catch(e){r={error:"sem ligação ao servidor"}}
  busy=false;if(r.check){c=r.check;rem=c.cooldown_remaining_s||0;fill(c)}
  if(r.error){chkMsg={uid,text:r.error};msg.textContent=r.error;paint();return}
  chkMsg={uid,text:`${r.requests} pedido(s) · ${r.new_scores} score(s) novo(s)${r.recategorized?" · perfil recalculado":""}${r.note?" · "+r.note:""}`};
  paint();show("p",uid)};
 row.append(btn,msg);box.append(l1,l2,l3,row);return box}


const AXL=[["aim","Aim"],["speed","Speed"],["stamina","Stamina"],["reading","Reading"]];
const KIND={novo:"novo",rejogar:"rejogar",tentar_de_novo:"tentar de novo"};
function recCard(uid){
 const box=el("div","card2"),head=el("div");head.appendChild(el("b","","Recomendar mapas"));
 box.appendChild(head);box.appendChild(el("div","sub","Escolhe a(s) skill(s) que queres melhorar. O resto é automático: mapas com accuracy esperada ≥ 88 % (ideal ~93 %) que te desafiem nessas skills, do estilo de jogadores parecidos; mapas já jogados voltam se a accuracy/pp prevista for bastante superior à atual."));
 const row=el("div","tools"),sel=new Set();
 AXL.forEach(([k,l])=>{const b=el("button","",l);b.onclick=()=>{if(sel.has(k)){sel.delete(k);b.classList.remove("on")}else{sel.add(k);b.classList.add("on")}};row.appendChild(b)});
 const go=el("button","go","Recomendar"),msg=el("span","sub");row.append(go,msg);box.appendChild(row);
 const out=el("div");box.appendChild(out);
 go.onclick=async()=>{if(!sel.size){msg.textContent="escolhe pelo menos uma skill";return}
  go.disabled=true;msg.textContent="a calcular…";out.replaceChildren();
  let r;try{r=await post("/api/recommend",{id:uid,skills:[...sel]})}catch(e){r={error:"sem ligação ao servidor"}}
  go.disabled=false;if(r.error){msg.textContent=r.error;return}
  msg.textContent=`${r.items.length} sugestões · candidatos: ${r.counts.novo} novos, ${r.counts.rejogar} a repetir, ${r.counts.tentar_de_novo} a tentar de novo`;
  const lv=r.player.levels;out.appendChild(el("div","sub","O teu nível (P90 dos melhores passes; 50 = mapa mediano): "+["aim","speed","stamina","reading"].map(a=>a+" "+lv[a].toFixed(0)).join(" · ")));
  const cols=[{h:"#",sort:x=>x.rank,render:x=>String(x.rank)},
   {h:"Mapa",sort:x=>x.label.toLowerCase(),num:false,cls:"wrap",render:x=>{const a=el("a","lk",x.label);a.href=x.url;a.target="_blank";a.rel="noopener noreferrer";const w=el("span");w.appendChild(a);w.appendChild(el("div","sub","ID do mapa: "+x.beatmap_id+(x.beatmapset_id?" · set "+x.beatmapset_id:"")));return w}},
   {h:"Nível",sort:x=>x.tier,num:false,render:x=>x.tier==="seguro"?"seguro":"arriscado"},{h:"Tipo",sort:x=>x.kind,num:false,render:x=>KIND[x.kind]||x.kind},{h:"★",sort:x=>x.stars,render:x=>x.stars.toFixed(2)},
   {h:"Desafio",sort:x=>x.delta[r.skills[0]],render:x=>r.skills.map(a=>a+" "+(x.delta[a]>=0?"+":"")+x.delta[a].toFixed(1)).join(" · ")},
   {h:"P(passar)",sort:x=>x.p_pass,render:x=>Math.round(x.p_pass*100)+" %"},{h:"Acc se passar",sort:x=>x.acc_pass,render:x=>(x.acc_pass*100).toFixed(1)+" %"},
   {h:"Acc atual",sort:x=>x.acc_cur,render:x=>x.acc_cur==null?"—":(x.acc_cur*100).toFixed(1)+" %"},
   {h:"pp est.",sort:x=>x.pp_gain_pct,render:x=>x.pp_gain_pct==null?"—":"+"+Math.round(x.pp_gain_pct)+" %"},
   {h:"Estilo",sort:x=>x.style_pct,render:x=>x.style_pct.toFixed(0)},
   {h:"Porquê",sort:x=>x.why,num:false,cls:"wrap",render:x=>{const d=el("span","sub",x.why);return d}},
   {h:"Feedback",sort:x=>0,render:x=>{const w=el("span"),done=t=>{w.replaceChildren(el("span","sub",t))};
     const y=el("button","","Serve"),n=el("button","","Não serve");
     y.onclick=async()=>{await post("/api/rec_feedback",{id:uid,beatmap_id:x.beatmap_id,verdict:"serve",skills:r.skills,kind:x.kind,score:x.score});done("obrigado: serve")};
     n.onclick=async()=>{await post("/api/rec_feedback",{id:uid,beatmap_id:x.beatmap_id,verdict:"nao_serve",skills:r.skills,kind:x.kind,score:x.score});done("obrigado: não serve")};
     const blk=async scope=>{const res=await post("/api/rec_block",{id:uid,beatmap_id:x.beatmap_id,beatmapset_id:x.beatmapset_id,scope});
      done(res.error?res.error:scope==="set"?"mapa bloqueado (não volta a ser recomendado)":"dificuldade bloqueada");if(!res.error)loadBlocks()};
     const b1=el("button","","Não recomendar o mapa"),b2=el("button","","só esta dificuldade");b1.onclick=()=>blk("set");b2.onclick=()=>blk("diff");
     w.append(y,n,el("br"),b1,b2);return w}}];
  r.items.forEach((x,i)=>x.rank=i+1);
  out.appendChild(sortable(cols,r.items,0,1));
  (r.notes||[]).forEach(t=>out.appendChild(el("div","sub","· "+t)))};
 const bl=el("div"),blkin=el("input"),bs=el("button","","Bloquear o mapa inteiro"),bd=el("button","","Só esta dificuldade"),bm=el("span","sub"),blist=el("div");
 blkin.placeholder="Link ou ID do mapa a não receber";blkin.style.minWidth="280px";
 bl.appendChild(el("div","sub","Mapas que não queres receber (preferência tua; podes bloquear na tabela acima ou colar aqui um link/ID):"));
 const brow=el("div","tools");brow.append(blkin,bs,bd,bm);bl.append(brow,blist);box.appendChild(bl);
 async function loadBlocks(){const r=await post("/api/rec_blocks",{id:uid});if(r.error){blist.replaceChildren(el("div","sub",r.error));return}
  if(!r.items.length){blist.replaceChildren(el("div","sub","Nenhum mapa bloqueado."));return}
  blist.replaceChildren(...r.items.map(b=>{const d=el("div","sub"),a=el("a","lk",b.label||("mapa "+(b.beatmapset_id||b.beatmap_id)));a.href=b.url;a.target="_blank";a.rel="noopener noreferrer";
   const u=el("button","","Desbloquear");u.style.marginLeft="8px";u.onclick=async()=>{await post("/api/rec_unblock",{id:uid,block_id:b.id});loadBlocks()};
   d.append(a,document.createTextNode(" · "+(b.scope==="set"?"mapa inteiro":"só esta dificuldade")+(b.beatmap_id?" · ID "+b.beatmap_id:"")),u);return d}))}
 const doRef=async scope=>{const ref=blkin.value.trim();if(!ref){bm.textContent="cola um link ou um ID";return}
  const r=await post("/api/rec_block",{id:uid,ref,scope});bm.textContent=r.error||(r.already?"já estava bloqueado: ":"bloqueado: ")+(r.label||"");if(!r.error)blkin.value="";loadBlocks()};
 bs.onclick=()=>doRef("set");bd.onclick=()=>doRef("diff");loadBlocks();
 return box}

function renderMap(r){
 const out=[],h=el("div");h.appendChild(el("p","big",r.label));
 h.appendChild(el("div","sub",`#${r.beatmap_id}${r.creator?" · mapper "+r.creator:""} · ${r.status||""} · ${n2(r.difficulty_rating)}★ (sem mods)${r.has_file?"":" · sem ficheiro .osu"}`));
 if(r.url){const a=el("a","lk","Abrir no osu!");a.href=r.url;a.target="_blank";a.rel="noopener noreferrer";h.appendChild(a)}
 if(r.note)h.appendChild(el("div","sub",r.note));out.push(h);
 out.push(el("h2","","Atributos por mods"));
 if(!r.variants.length)out.push(el("div","empty","Ainda não categorizado."));
 else{const cols=[{h:"Mods",sort:x=>x.mods,num:false,render:x=>x.mods||"NM"},
  {h:"★",sort:x=>x.axes&&x.axes.stars?x.axes.stars.score:null,render:x=>x.status!=="ok"?el("span","dim",x.status+(x.error?" · "+x.error:"")):axCell(x.axes&&x.axes.stars)}];
  AX.slice(0,4).forEach(([k,l])=>cols.push({h:l,sort:x=>x.axes&&x.axes[k]?x.axes[k].score:null,render:x=>axCell(x.axes&&x.axes[k])}));
  const rw=(k,f)=>x=>x.raw&&x.raw[k]!=null?f(x.raw[k]):"—";
  cols.push({h:"★ real",sort:x=>x.raw&&x.raw.stars,render:rw("stars",n2)},{h:"aim",sort:x=>x.raw&&x.raw.aim,render:rw("aim",n2)},{h:"speed",sort:x=>x.raw&&x.raw.speed,render:rw("speed",n2)},
   {h:"reading",sort:x=>x.raw&&x.raw.reading,render:rw("reading",n2)},{h:"obj/s",sort:x=>x.raw&&x.raw.density,render:rw("density",n2)},
   {h:"AR",sort:x=>x.raw&&x.raw.ar,render:rw("ar",n1)},{h:"CS",sort:x=>x.raw&&x.raw.cs,render:rw("cs",n1)},{h:"OD",sort:x=>x.raw&&x.raw.od,render:rw("od",n1)},{h:"HP",sort:x=>x.raw&&x.raw.hp,render:rw("hp",n1)},{h:"Objetos",sort:x=>x.raw&&x.raw.n_objects,render:rw("n_objects",n0)});
  out.push(sortable(cols,r.variants,0,1));
  out.push(el("div","sub","As notas são abertas (50 = mapa mediano da pool, sem teto); “top x%” no tooltip. Aim/Speed/Reading variam com os mods; Stamina é nomod. Passa o rato sobre a nota."))}
 out.push(el("h2","","Plays de jogadores ("+r.plays.length+")"));
 const cols=[{h:"Jogador",sort:x=>x.username.toLowerCase(),num:false,render:x=>link(x.username,()=>show("p",x.user_id))},
  {h:"pp jogador",sort:x=>x.player_pp,render:x=>x.player_pp==null?"—":n0(x.player_pp)},{h:"Mods",sort:x=>x.mods,num:false,render:x=>x.mods||"NM"},
  {h:"Data",sort:x=>x.ended_at,num:false,render:x=>dt(x.ended_at)},{h:"Acc",sort:x=>x.accuracy,render:x=>x.accuracy==null?"—":(x.accuracy*100).toFixed(2)+"%"},
  {h:"pp",sort:x=>x.pp,render:x=>x.pp==null?"—":n0(x.pp)},{h:"Resultado",sort:x=>x.passed?1:0,render:x=>el("span",x.passed?"pass":"fail",x.passed?"pass "+(x.rank||""):"fail")},
  {h:"Combo",sort:x=>x.max_combo,render:x=>n0(x.max_combo)}];
 out.push(r.plays.length?sortable(cols,r.plays,5,-1):el("div","empty","Sem plays."));return out}

function fromHash(){const m=(location.hash||"").match(/^#([pm])\/(\d+)$/);
 if(!m)return false;const id=+m[2];if(m[1]==="p")selP=id;else selM=id;
 if(mode!==m[1])setMode(m[1]);show(m[1],id);return true}
window.addEventListener("hashchange",fromHash);
if(!fromHash())setMode(mode);
</script></body></html>"""


def actions(explorer: Explorer, checker: Any | None = None, recommender: Any | None = None) -> dict[str, Callable[[dict], Any]]:
    def _int(body: dict, key: str = "id") -> int | None:
        try:
            return int(body.get(key))
        except (TypeError, ValueError):
            return None

    def player(body: dict) -> dict:
        uid = _int(body)
        res = explorer.player(uid) if uid is not None else None
        return res if res is not None else {"error": "jogador não encontrado"}

    def beatmap(body: dict) -> dict:
        bid = _int(body)
        res = explorer.map(bid) if bid is not None else None
        return res if res is not None else {"error": "mapa não encontrado"}

    def check(body: dict) -> dict:
        uid = _int(body)
        if uid is None:
            return {"error": "jogador inválido"}
        if checker is None:
            return {"error": "verificação manual indisponível neste painel"}
        return checker.force(uid)

    def recommend(body: dict) -> dict:
        uid = _int(body)
        if uid is None:
            return {"error": "jogador inválido"}
        if recommender is None:
            return {"error": "recomendador indisponível neste painel"}
        skills = body.get("skills") if isinstance(body.get("skills"), list) else []
        return recommender.recommend(uid, [str(x) for x in skills])

    def rec_feedback(body: dict) -> dict:
        uid, bid = _int(body), _int(body, "beatmap_id")
        if uid is None or bid is None or recommender is None:
            return {"error": "pedido inválido"}
        skills = body.get("skills") if isinstance(body.get("skills"), list) else []
        try:
            score = float(body["score"]) if body.get("score") is not None else None
        except (TypeError, ValueError):
            score = None
        return recommender.feedback(uid, bid, str(body.get("verdict") or ""), [str(x) for x in skills], str(body.get("kind") or ""), score)

    def rec_block(body: dict) -> dict:
        from ..recommend.core import parse_map_ref

        uid = _int(body)
        if uid is None or recommender is None:
            return {"error": "pedido inválido"}
        bid = sid = None
        if body.get("ref"):
            bid, sid = parse_map_ref(body.get("ref"))
            if bid is None and sid is None:
                return {"error": "não percebi o mapa: cola um link do osu! ou o ID"}
        else:
            bid = _int(body, "beatmap_id")
            sid = _int(body, "beatmapset_id")
        return recommender.block_map(uid, beatmap_id=bid, beatmapset_id=sid or None, scope=str(body.get("scope") or "set"))

    def rec_unblock(body: dict) -> dict:
        uid, bid = _int(body), _int(body, "block_id")
        if uid is None or bid is None or recommender is None:
            return {"error": "pedido inválido"}
        return recommender.unblock(uid, bid)

    def rec_blocks(body: dict) -> dict:
        uid = _int(body)
        if uid is None or recommender is None:
            return {"error": "pedido inválido"}
        return {"items": recommender.blocks(uid)}

    return {
        "/api/recommend": recommend,
        "/api/rec_feedback": rec_feedback,
        "/api/rec_block": rec_block,
        "/api/rec_unblock": rec_unblock,
        "/api/rec_blocks": rec_blocks,
        "/api/check": check,
        "/api/players": lambda body: explorer.search_players(str(body.get("q") or "")),
        "/api/maps": lambda body: explorer.search_maps(str(body.get("q") or "")),
        "/api/player": player,
        "/api/map": beatmap,
    }
