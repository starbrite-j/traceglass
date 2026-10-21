/* Local-only graph workspace. Screen distance never represents evidence. */
(() => {
  'use strict';
  const host=document.querySelector('#constellationApp');
  host.parentElement.classList.add('constellation-ready');
  host.innerHTML=`
    <header class="stellar-head"><div><small>TRACEGLASS / OBSERVATORY</small><h2 id="stellarTitle">The conversation atlas</h2></div><button id="stellarAtlas" class="chrome-button">↖ ATLAS</button></header>
    <div class="stellar-tools">
      <label id="atlasSearchLabel">FIND A CONVERSATION <input id="atlasSearch" placeholder="Title or conversation ID…"></label>
      <div id="stellarGraphTools" hidden>
        <button id="stellarMore">EXPAND +600</button><button id="stellarTrace">TRACE TO ROOT</button>
        <label>COMPARE COPY <select id="stellarCompare"><option value="0">Selected source only</option></select></label>
        <label><input id="stellarMetadata" type="checkbox" checked> Metadata links</label>
        <button id="stellarFit">FIT</button><button id="stellarFocus">CENTER SELECTION</button>
      </div>
      <button id="stellarCancel" hidden>CANCEL LOAD</button>
    </div>
    <p id="stellarStatus" role="status" aria-live="polite">Choose a conversation to unfold its recorded lineage.</p>
    <div id="stellarAtlasPane"><div id="stellarClusters"></div><footer class="stellar-pages"><button id="atlasPrev">← PREVIOUS</button><span id="atlasPage"></span><button id="atlasNext">NEXT →</button></footer></div>
    <div id="stellarGraphPane" hidden>
      <div class="stellar-canvas-wrap"><canvas id="stellarCanvas" tabindex="0" aria-label="Lineage constellation. Drag to pan; scroll to zoom. Use the record list to select nodes with a keyboard."></canvas>
        <div class="stellar-legend"><span>● User</span><span>● Assistant</span><span>● System / tool</span><span>— Outer parent</span><span>┄ Other copy</span><span>·· Metadata</span><span>◇ Missing / unloaded target</span></div>
        <div class="stellar-zoom"><button id="stellarZoomIn" aria-label="Zoom constellation in">+</button><button id="stellarZoomOut" aria-label="Zoom constellation out">−</button></div>
        <p class="stellar-layout-note">POSITION IS LAYOUT, NOT EVIDENCE · ARROWS POINT PARENT → CHILD</p>
      </div>
      <aside class="stellar-inspect"><div id="stellarSelection">Select a point to inspect its connections.</div>
        <label>LOADED RECORDS <input id="stellarNodeSearch" placeholder="ID, role, or text…"></label><div id="stellarNodeList"></div>
        <footer class="stellar-pages"><button id="nodePrev">←</button><span id="nodePage"></span><button id="nodeNext">→</button></footer>
      </aside>
    </div>
    <section id="stellarTracePane" hidden aria-label="Extended parent trace">
      <header><strong>Literal parent walk</strong><label>FOLLOW <select id="stellarTraceField"><option value="outer">outer parent</option><option value="metadata">metadata.parent_id</option></select></label><button id="traceStart">START / RESTART</button><button id="traceStop">STOP</button><button id="traceClose">CLOSE</button></header>
      <p id="stellarTraceStatus" role="status"></p><div id="stellarTraceList"></div>
      <footer class="stellar-pages"><button id="tracePrev">←</button><span id="tracePage"></span><button id="traceNext">→</button><button id="traceContinue" hidden>CONTINUE</button></footer>
    </section>`;
  const el=id=>document.getElementById(id), esc=escapeHtml;
  // Each nesting layer has its own page; jumping never expands a graph or trace.
  for(const prefix of ['atlas','node','trace']){
    const form=document.createElement('form');form.id=prefix+'Jump';form.className='page-jump';
    const name={atlas:'Conversation atlas',node:'Loaded graph records',trace:'Parent trace'}[prefix];
    form.innerHTML=`<label>PAGE <input id="${prefix}PageInput" type="text" inputmode="numeric" value="1" aria-label="${name} page number"></label><button type="submit">GO</button>`;
    el(prefix+'Page').before(form);
  }
  let mode='atlas', page=1, pages=1, dataset='', sourceFilter='', record=null, data=null, limit=600, compare=0;
  let request=null, serial=0, layout=null, frame=0, selected=null, hovered=null, nodes=[], edges=[], positions=new Map(), ancestors=new Set();
  let camera={x:0,y:0,z:1}, width=1,height=1, nodePage=1, tracePage=1, tracing=false, traceToken=0, traceRequest=null;
  let traceSteps=[], traceSeen=new Set(), traceCursor=null, traceField='outer', traceScope=null;
  const canvas=el('stellarCanvas'), ctx=canvas.getContext('2d'), cache=new Map();
  const color=n=>n.role==='user'?'#dcba77':n.role==='assistant'?'#ab9bf2':n.role==='system'?'#f08ebf':'#66d9dc';
  function hash(s){let h=2166136261;for(const c of s)h=Math.imul(h^c.charCodeAt(0),16777619);return h>>>0;}
  function status(text){el('stellarStatus').textContent=text;}
  function busy(on){el('stellarCancel').hidden=!on;el('stellarMore').disabled=on||!(data?.remaining||data?.gaps.some(g=>g.kind==='other'&&g.state==='not_loaded'))||limit>=6000;el('stellarTrace').disabled=on||!data?.nodes.length;el('stellarCompare').disabled=on||!data;}
  function stopLayout(){if(layout)layout.terminate();layout=null;cancelAnimationFrame(frame);}
  function stopTrace(){tracing=false;traceToken++;traceRequest?.abort();el('traceStop').disabled=true;}
  function show(view){mode=view;el('stellarAtlasPane').hidden=view!=='atlas';el('stellarGraphPane').hidden=view!=='graph';el('stellarGraphTools').hidden=view!=='graph';el('atlasSearchLabel').hidden=view!=='atlas';el('stellarAtlas').hidden=view==='atlas';if(view==='atlas')stopLayout();}
  async function get(url, signal){const r=await fetch(url,{signal});const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d;}
  function remember(){if(!record||!data)return;cache.set(record.source_id+':'+record.conversation_id,{camera:{...camera},positions:new Map(positions),selected,limit,compare});if(cache.size>5)cache.delete(cache.keys().next().value);}
  async function atlas(reset=false){
    remember();stopTrace();el('stellarTracePane').hidden=true;show('atlas');request?.abort();request=new AbortController();const s=++serial;
    if(reset)page=1;dataset=state.datasetId;sourceFilter=state.sourceId;el('stellarTitle').textContent='The conversation atlas';status('Loading conversation copies…');busy(true);
    try{const d=await get('/api/atlas?'+new URLSearchParams({dataset_id:dataset||0,source_id:sourceFilter||0,page,q:el('atlasSearch').value}),request.signal);if(s!==serial)return;
      page=d.page;pages=d.pages;el('atlasPageInput').value=page;el('atlasPage').textContent=`${d.total.toLocaleString()} copies · ${pages} pages`;el('atlasPrev').disabled=page<=1;el('atlasNext').disabled=page>=pages;
      el('stellarClusters').innerHTML=d.items.map((n,i)=>{
        const seed=hash(n.conversation_id),dots=Array.from({length:Math.min(85,Math.max(1,n.nodes))},(_,j)=>{const angle=j*2.39996+seed%6,r=Math.sqrt(j)*4.8;return `<circle cx="${95+Math.cos(angle)*r}" cy="${55+Math.sin(angle)*r*.66}" r="${j%7===0?1.8:1}" opacity="${.3+(j%5)*.14}"/>`;}).join('');
        return `<button class="stellar-cluster palette-${seed%4}" data-cluster="${i}" ${n.pk?'':'disabled'}><svg viewBox="0 0 190 110" aria-hidden="true">${dots}</svg><span class="cluster-count">${n.nodes.toLocaleString()} NODES</span><strong>${esc(n.title||'Untitled')}</strong><small>Source ${n.source_id} · ${esc(n.conversation_id.slice(0,13))}…</small></button>`;
      }).join('')||'<p class="stellar-none">No conversations match. Try another title or dataset.</p>';
      host.querySelectorAll('[data-cluster]').forEach(b=>b.onclick=async()=>{clearTimeout(searchTimer);const n=d.items[Number(b.dataset.cluster)];await openDetail(n.pk);closeDetail();open(state.selected);});
      status('Each cluster is a conversation copy. Dots are a compact count preview, not individual records. No connections are inferred between conversations.');
    }catch(e){if(e.name!=='AbortError')status('Could not load atlas: '+e.message);}finally{if(s===serial)busy(false);}
  }
  async function open(r){
    clearTimeout(searchTimer);
    if(!r)return atlas();
    const key=r.source_id+':'+r.conversation_id, old=record&&record.source_id+':'+record.conversation_id;
    if(mode==='graph'&&old===key&&selected===r.node_id&&data)return;
    remember();stopTrace();el('stellarTracePane').hidden=true;record={source_id:r.source_id,conversation_id:r.conversation_id,node_id:r.node_id};
    if(key!==old){data=null;nodes=[];edges=[];el('stellarSelection').textContent='Loading selected source…';el('stellarNodeList').textContent='';ctx.clearRect(0,0,width,height);}
    const saved=cache.get(key);limit=saved?.limit||600;compare=saved?.compare||0;positions=saved?.positions||new Map();camera=saved?.camera||{x:0,y:0,z:1};selected=r.node_id;nodePage=1;
    show('graph');await load(!saved);
  }
  async function load(fit=false){
    request?.abort();request=new AbortController();const s=++serial;busy(true);status('Loading source-scoped lineage…');
    try{const d=await get('/api/constellation?'+new URLSearchParams({...record,focus:selected||record.node_id,limit,compare_source:compare}),request.signal);if(s!==serial)return;
      stopLayout();data=d;nodes=d.nodes;selected=nodes.some(n=>n.id===selected)?selected:d.focus;
      el('stellarTitle').textContent=d.title||'Untitled conversation';el('stellarCompare').innerHTML='<option value="0">Selected source only</option>'+d.versions.filter(v=>v.source_id!==d.source_id).map(v=>`<option value="${v.source_id}" ${v.source_id===compare?'selected':''}>D${v.dataset_id} / source ${v.source_id} · ${esc(fileName(v.path))}</option>`).join('');
      status(`${d.loaded_active.toLocaleString()} / ${d.total_active.toLocaleString()} source nodes loaded · ${d.remaining.toLocaleString()} not loaded · ${d.nodes.filter(n=>n.supplemental).length} other-copy nodes · source ${d.source_id}`);
      el('stellarMore').textContent=limit>=6000?'6,000-NODE WINDOW · REFOCUS':'EXPAND +600';
      resize();startLayout(fit);select(selected);renderNodes();
    }catch(e){if(e.name!=='AbortError')status('Could not load graph: '+e.message);}finally{if(s===serial)busy(false);}
  }
  function resize(){const r=canvas.getBoundingClientRect();width=Math.max(1,r.width);height=Math.max(1,r.height);const dpr=Math.min(2,devicePixelRatio||1);canvas.width=width*dpr;canvas.height=height*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);draw();}
  function startLayout(fit){
    const fixedIds=nodes.filter(n=>positions.has(n.id)).map(n=>n.id);
    // Topological traversal seeds neighboring records together, not randomly across the canvas.
    const by=new Map(nodes.map(n=>[n.id,n])), kids=new Map(), visited=new Set(), ordered=[];
    nodes.forEach(n=>{if(by.has(n.parent_id)){if(!kids.has(n.parent_id))kids.set(n.parent_id,[]);kids.get(n.parent_id).push(n.id);}});
    const starts=nodes.filter(n=>!by.has(n.parent_id)).map(n=>n.id).concat(nodes.map(n=>n.id));
    for(const start of starts){const stack=[start];while(stack.length){const id=stack.pop();if(visited.has(id))continue;visited.add(id);ordered.push(id);stack.push(...(kids.get(id)||[]).slice().reverse());}}
    ordered.forEach((id,i)=>{if(!positions.has(id)){const index=by.get(id).layout_index??i,a=.85*Math.sqrt(index+1),r=30*Math.sqrt(index+1);positions.set(id,{x:Math.cos(a)*r,y:Math.sin(a)*r});}});
    edges=data.edges;if(fit)fitGraph();
    layout=new Worker('/constellation-worker.js');layout.onmessage=e=>{e.data.positions.forEach(p=>positions.set(p.id,p));if(e.data.done&&fit)fitGraph();else draw();};
    layout.postMessage({nodes:nodes.map(n=>({id:n.id,...positions.get(n.id)})),edges:edges.filter(e=>e.kind==='outer'),fixed:selected,fixedIds});draw();
  }
  function fitGraph(){if(!nodes.length)return;const ps=nodes.map(n=>positions.get(n.id)).filter(Boolean);const xs=ps.map(p=>p.x),ys=ps.map(p=>p.y);const minX=Math.min(...xs)-60,maxX=Math.max(...xs)+60,minY=Math.min(...ys)-60,maxY=Math.max(...ys)+60;camera.z=Math.min(2.5,width/(maxX-minX),height/(maxY-minY))*.85;camera.x=width/2-(minX+maxX)/2*camera.z;camera.y=height/2-(minY+maxY)/2*camera.z;draw();}
  function center(){const p=positions.get(selected);if(p){camera.x=width/2-p.x*camera.z;camera.y=height/2-p.y*camera.z;draw();}}
  function parentLabel(n){if(n.parent_type===null)return 'OUTER PARENT FIELD ABSENT';if(n.parent_type==='null')return 'ROOT: outer parent is explicitly null';if(n.parent_type!=='text')return 'INVALID OUTER PARENT TYPE: '+esc(n.parent_type);return 'Outer parent: '+esc(n.parent_id)+(n.parent_lists_child===false?'<br><b class="stellar-warning">BACKLINK CONFLICT: parent does not list this child</b>':'');}
  function select(id){selected=id;const by=new Map(nodes.map(n=>[n.id,n]));ancestors=new Set();let cur=id;while(by.has(cur)&&!ancestors.has(cur)){ancestors.add(cur);cur=by.get(cur).parent_id;}
    const n=by.get(id);if(!n)return;const gaps=data.gaps.filter(g=>g.child===id);const date=n.display_time==null?'No valid timestamp':fmtTime(n.display_time).main+' · '+fmtTime(n.display_time).sub;
    el('stellarSelection').innerHTML=`<small class="eyebrow">${n.supplemental?'OTHER COPY':'SELECTED SOURCE'} · ${esc(n.role||'root')} ${n.hidden?'· HIDDEN':''}</small><h3>${esc(n.preview||'[Empty record]')}</h3><p>${esc(date)}</p><code>${esc(n.id)}</code><p>${parentLabel(n)}</p><p>${n.metadata_parent_present?'Metadata parent: '+esc(n.metadata_parent===null?'NULL':String(n.metadata_parent)):'Metadata parent field absent'}</p>${n.mismatch?'<b class="stellar-warning">PARENT FIELDS DIFFER</b>':''}${gaps.map(g=>`<p class="stellar-warning">◇ ${esc(g.kind)} · source ${g.source_id}: ${g.state==='not_loaded'?'NOT LOADED YET':g.state==='invalid'?'INVALID REFERENCE':'TARGET ABSENT IN THIS SOURCE'}<code>${esc(g.target)}</code></p>`).join('')}<p>${n.unloaded_children} direct children not loaded</p><button id="stellarInspectRecord">OPEN RECORD / RAW JSON</button><button id="stellarRefocus">EXPAND FROM THIS NODE</button>`;
    el('stellarInspectRecord').onclick=()=>openDetail(n.pk);el('stellarRefocus').onclick=()=>{if(n.supplemental){openDetail(n.pk).then(()=>{closeDetail();open(state.selected);});}else{limit=600;record.node_id=n.id;load(false);}};draw();
  }
  function renderNodes(){const q=el('stellarNodeSearch').value.toLowerCase();const list=nodes.filter(n=>(n.id+' '+n.role+' '+n.preview).toLowerCase().includes(q));const pages=Math.max(1,Math.ceil(list.length/60));nodePage=Math.min(nodePage,pages);el('nodePageInput').value=nodePage;el('stellarNodeList').innerHTML=list.slice((nodePage-1)*60,nodePage*60).map(n=>`<button data-node="${esc(n.id)}"><i class="dot-${esc(n.role||'root')}"></i><span>${esc(n.role||'root')} ${n.hidden?'· HIDDEN':''}<small>${esc(n.id.slice(0,12))} · ${esc(n.preview||'[empty]')}</small></span></button>`).join('');host.querySelectorAll('[data-node]').forEach(b=>b.onclick=()=>{select(b.dataset.node);center();});el('nodePage').textContent=`${nodePage} / ${pages} · ${list.length}`;el('nodePrev').disabled=nodePage===1;el('nodeNext').disabled=nodePage===pages;}
  function draw(){if(mode!=='graph'||!data)return;ctx.clearRect(0,0,width,height);ctx.save();ctx.translate(camera.x,camera.y);ctx.scale(camera.z,camera.z);
    for(const e of edges){if(e.kind==='metadata'&&!el('stellarMetadata').checked)continue;const a=positions.get(e.source),b=positions.get(e.target);if(!a||!b)continue;const hot=ancestors.has(e.source)&&ancestors.has(e.target)&&e.kind==='outer';ctx.globalAlpha=hot?.9:selected?.17:.45;ctx.strokeStyle=e.kind==='other'?'#62dce7':e.kind==='metadata'?'#ed82bb':hot?'#f3d598':'#8c97af';ctx.lineWidth=(hot?1.7:.7)/camera.z;ctx.setLineDash(e.kind==='outer'?[]:e.kind==='other'?[7/camera.z,5/camera.z]:[2/camera.z,4/camera.z]);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();if(camera.z>.6){const dx=b.x-a.x,dy=b.y-a.y,angle=Math.atan2(dy,dx),size=4/camera.z,x=b.x-Math.cos(angle)*7/camera.z,y=b.y-Math.sin(angle)*7/camera.z;ctx.beginPath();ctx.moveTo(x-Math.cos(angle-.45)*size,y-Math.sin(angle-.45)*size);ctx.lineTo(x,y);ctx.lineTo(x-Math.cos(angle+.45)*size,y-Math.sin(angle+.45)*size);ctx.stroke();}}
    ctx.setLineDash([]);
    for(const n of nodes){const p=positions.get(n.id);if(!p)continue;const hot=n.id===selected||n.id===hovered;ctx.globalAlpha=hot||ancestors.has(n.id)?1:.65;ctx.fillStyle=color(n);ctx.beginPath();ctx.arc(p.x,p.y,(hot?6:3)/Math.sqrt(camera.z),0,Math.PI*2);ctx.fill();if(n.mismatch||n.hidden||n.supplemental){ctx.strokeStyle=n.mismatch?'#ff7398':n.hidden?'#e69bc8':'#66e5ff';ctx.lineWidth=1/camera.z;ctx.beginPath();ctx.arc(p.x,p.y,(hot?10:6)/Math.sqrt(camera.z),0,Math.PI*2);ctx.stroke();}if(hot||(camera.z>1.8&&nodes.length<250)){ctx.globalAlpha=1;ctx.font=`${11/camera.z}px ui-monospace,monospace`;ctx.fillStyle='#e0e5ed';ctx.fillText((n.role||'root')+' · '+n.id.slice(0,8),p.x+12/camera.z,p.y-9/camera.z);}}
    // Gap markers are evidence placeholders, not invented message nodes.
    let gapLine=0;for(const g of data.gaps.filter(g=>g.child===selected)){if(g.kind==='metadata'&&!el('stellarMetadata').checked)continue;const p=positions.get(g.child);if(!p)continue;ctx.globalAlpha=1;ctx.fillStyle='#ff91bd';ctx.font=`${13/camera.z}px ui-monospace`;ctx.fillText('◇ '+(g.state==='not_loaded'?'not loaded':'missing / invalid')+' '+g.kind,p.x+14/camera.z,p.y+(23+gapLine++*17)/camera.z);}
    ctx.restore();ctx.globalAlpha=1;
  }
  function zoom(f,x=width/2,y=height/2){const z=Math.max(.04,Math.min(8,camera.z*f));camera.x=x-(x-camera.x)*z/camera.z;camera.y=y-(y-camera.y)*z/camera.z;camera.z=z;draw();}
  function hit(x,y){let best=null,dist=18;for(const n of nodes){const p=positions.get(n.id);if(!p)continue;const d=Math.hypot(p.x*camera.z+camera.x-x,p.y*camera.z+camera.y-y);if(d<dist){dist=d;best=n.id;}}return best;}
  let drag=null;
  canvas.onpointerdown=e=>{drag={x:e.offsetX,y:e.offsetY,cx:camera.x,cy:camera.y,moved:false};canvas.setPointerCapture(e.pointerId);};
  canvas.onpointermove=e=>{if(drag){const dx=e.offsetX-drag.x,dy=e.offsetY-drag.y;drag.moved ||= Math.hypot(dx,dy)>4;camera.x=drag.cx+dx;camera.y=drag.cy+dy;draw();}else{hovered=hit(e.offsetX,e.offsetY);canvas.style.cursor=hovered?'pointer':'grab';draw();}};
  canvas.onpointerup=e=>{if(drag&&!drag.moved){const id=hit(e.offsetX,e.offsetY);if(id)select(id);}drag=null;};canvas.onpointercancel=()=>drag=null;
  canvas.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY<0?1.12:1/1.12,e.offsetX,e.offsetY);},{passive:false});
  canvas.onkeydown=e=>{if(e.key==='+'||e.key==='='){e.preventDefault();zoom(1.2);}if(e.key==='-'){e.preventDefault();zoom(1/1.2);}if(e.key==='Home'){e.preventDefault();center();}const delta={ArrowLeft:[30,0],ArrowRight:[-30,0],ArrowUp:[0,30],ArrowDown:[0,-30]}[e.key];if(delta){e.preventDefault();camera.x+=delta[0];camera.y+=delta[1];draw();}};
  function renderTrace(){const pages=Math.max(1,Math.ceil(traceSteps.length/60));tracePage=Math.min(tracePage,pages);el('tracePageInput').value=tracePage;el('stellarTraceList').innerHTML=traceSteps.slice((tracePage-1)*60,tracePage*60).map((n,i)=>`<button data-step="${n.pk}"><small>${(tracePage-1)*60+i+1} · ${esc(n.role||'root')}${n.hidden?' · HIDDEN':''}</small><code>${esc(n.node_id)}</code>${n.parent_lists_child===false?'<b>BACKLINK CONFLICT</b>':''}</button>`).join('');host.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>openDetail(Number(b.dataset.step)));el('tracePage').textContent=`${tracePage} / ${pages} · ${traceSteps.length.toLocaleString()} traced`;el('tracePrev').disabled=tracePage===1;el('traceNext').disabled=tracePage===pages;}
  async function trace(resume=false){
    stopTrace();tracing=true;const token=traceToken;el('traceStop').disabled=false;el('stellarTracePane').hidden=false;el('traceContinue').hidden=true;
    const n=nodes.find(n=>n.id===selected);if(!n){stopTrace();return;}
    if(!resume){traceSteps=[];traceSeen=new Set();traceCursor=n.id;traceField=el('stellarTraceField').value;tracePage=1;traceScope={source_id:n.source_id,conversation_id:record.conversation_id};}
    const scope=traceScope, budget=traceSteps.length+50000;let reason='stopped';
    try{while(tracing&&token===traceToken&&traceCursor&&traceSteps.length<budget){
      el('stellarTraceStatus').textContent=`Tracing ${traceField} in source ${scope.source_id} · ${traceSteps.length.toLocaleString()} nodes. Export structure only.`;
      traceRequest=new AbortController();const result=await get('/api/parent-trace?'+new URLSearchParams({...scope,node_id:traceCursor,limit:500}),traceRequest.signal);if(token!==traceToken)return;const walk=result[traceField];
      let cycle=false;for(const step of walk.steps){if(traceSeen.has(step.node_id)){cycle=true;break;}traceSeen.add(step.node_id);traceSteps.push(step);}
      renderTrace();if(cycle){reason='cycle detected across batches';traceCursor=null;break;}
      if(walk.stop.reason!=='limit'){reason=({null_parent:'Reached explicit null parent (root)',missing_field:'Field absent — not a root declaration',target_not_in_source:'Target absent in this source',cycle:'Cycle detected',invalid_parent_value:'Invalid parent value'})[walk.stop.reason]||walk.stop.reason;traceCursor=null;break;}
      traceCursor=walk.stop.node_id;await new Promise(resolve=>setTimeout(resolve,0));
    }}catch(e){if(e.name!=='AbortError')reason='Trace failed: '+e.message;else reason='Stopped by you';}finally{if(token===traceToken){tracing=false;el('traceStop').disabled=true;el('stellarTraceStatus').textContent=`${traceSteps.length.toLocaleString()} nodes · ${traceCursor?(traceSteps.length>=50000?'50,000-node safety boundary; not a root':reason):reason}`;el('traceContinue').hidden=!traceCursor;}}
  }
  el('stellarAtlas').onclick=()=>atlas();el('stellarMore').onclick=()=>{limit=Math.min(6000,limit+600);load(false);};el('stellarCompare').onchange=e=>{compare=Number(e.target.value);load(false);};el('stellarMetadata').onchange=draw;
  el('stellarFit').onclick=fitGraph;el('stellarFocus').onclick=center;el('stellarZoomIn').onclick=()=>zoom(1.3);el('stellarZoomOut').onclick=()=>zoom(1/1.3);
  el('stellarCancel').onclick=()=>{request?.abort();status('Load cancelled. Previously loaded records remain available.');};
  let searchTimer;el('atlasSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>atlas(true),250);};el('atlasPrev').onclick=()=>{page--;atlas();};el('atlasNext').onclick=()=>{page++;atlas();};
  el('stellarNodeSearch').oninput=()=>{nodePage=1;renderNodes();};el('nodePrev').onclick=()=>{nodePage--;renderNodes();};el('nodeNext').onclick=()=>{nodePage++;renderNodes();};
  el('stellarTrace').onclick=()=>trace();el('traceStart').onclick=()=>trace();el('traceContinue').onclick=()=>trace(true);el('traceStop').onclick=()=>{stopTrace();el('stellarTraceStatus').textContent=`Stopped by you · ${traceSteps.length} nodes retained`;el('traceContinue').hidden=!traceCursor;};el('traceClose').onclick=()=>{stopTrace();el('stellarTracePane').hidden=true;};
  el('tracePrev').onclick=()=>{tracePage--;renderTrace();};el('traceNext').onclick=()=>{tracePage++;renderTrace();};
  bindPageJump('atlasJump','atlasPageInput',()=>pages,p=>{page=p;atlas();});
  bindPageJump('nodeJump','nodePageInput',()=>Math.ceil(nodes.filter(n=>(n.id+' '+n.role+' '+n.preview).toLowerCase().includes(el('stellarNodeSearch').value.toLowerCase())).length/60),p=>{nodePage=p;renderNodes();});
  bindPageJump('traceJump','tracePageInput',()=>Math.ceil(traceSteps.length/60),p=>{tracePage=p;renderTrace();});
  new ResizeObserver(resize).observe(canvas);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLayout();});
  window.traceglassAtlas={open,atlas,refreshTime:()=>{if(mode==='graph'&&data)select(selected);},focusSearch:()=>el(mode==='atlas'?'atlasSearch':'stellarNodeSearch').focus()};
  const selectDataset=el('datasetSelect');selectDataset.addEventListener('change',()=>{if(state.view==='lineage'){record=null;data=null;atlas(true);}});
})();
