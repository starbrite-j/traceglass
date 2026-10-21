"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = {
  page: 1, pageSize: 100, query: "", searchMode: "auto", role: "", anomaly: "", sourceId: "", datasetId: "",
  view: "ledger", timeMode: "local", total: 0, pages: 1, selected: null,
  sources: [], exact: null, json: { query: "", offset: 0, length: 96 * 1024, match: 0, data: null },
  stats: null, graph: { scale: 1, x: 0, y: 0, dragging: false, sx: 0, sy: 0 }
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

function compactNumber(n) { return new Intl.NumberFormat(undefined, { notation: n > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(n || 0); }
function fileName(path) { return String(path || "").split("/").pop(); }
function shortId(id, n = 9) { if (!id) return "∅"; return String(id).length > n ? String(id).slice(0,n) + "…" : String(id); }
function fmtBytes(n) { if (!n) return "0 B"; const u=["B","KB","MB","GB"]; const i=Math.min(u.length-1,Math.floor(Math.log(n)/Math.log(1024))); return `${(n/1024**i).toFixed(i?1:0)} ${u[i]}`; }
function highlightedLiteral(text, query) {
  if(!query)return escapeHtml(text);
  const source=String(text??''),needle=String(query).toLowerCase();
  if(!needle)return escapeHtml(source);
  let out='',cursor=0,lower=source.toLowerCase(),found;
  while((found=lower.indexOf(needle,cursor))>=0){out+=escapeHtml(source.slice(cursor,found));out+=`<mark>${escapeHtml(source.slice(found,found+query.length))}</mark>`;cursor=found+query.length}
  return out+escapeHtml(source.slice(cursor));
}
function fmtTime(epoch) {
  if (epoch == null || !Number.isFinite(Number(epoch))) return { main: "NO VALID TIME", sub: "RAW VALUE PRESERVED" };
  const date = new Date(epoch * 1000);
  if (state.timeMode === "utc") return { main: date.toISOString().slice(0,10), sub: date.toISOString().slice(11,23) + " UTC" };
  return { main: new Intl.DateTimeFormat(undefined,{year:"numeric",month:"short",day:"2-digit"}).format(date), sub: new Intl.DateTimeFormat(undefined,{hour:"numeric",minute:"2-digit",second:"2-digit",fractionalSecondDigits:3}).format(date) };
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

let toastTimer;
function toast(message) { const el=$("#toast"); el.textContent=message; el.classList.add("show"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove("show"),2600); }

function bindPageJump(formId, inputId, maxPage, jump) {
  document.getElementById(formId).onsubmit=e=>{
    e.preventDefault();const input=document.getElementById(inputId),text=input.value.trim();
    if(!/^\d+$/.test(text)||!Number.isSafeInteger(Number(text))||Number(text)<1){toast('Enter a whole page number starting at 1');return;}
    const page=Math.min(Number(text),Math.max(1,maxPage()));input.value=String(page);jump(page);
  };
}
function showSearchScope(scope) {
  if(state.view!=='ledger')return;
  const modes={none:'No search query',auto:'Automatic search',text:'Text + titles · indexed phrase search',ids:'Record IDs · literal substring',metadata:'Metadata · literal text (not message text)'};
  $('#searchScopeNote').textContent=`${modes[scope?.effective_mode]||'Indexed search'} · dataset ${state.datasetId||'all'}${state.sourceId?' / source '+state.sourceId:''} · current filters apply. Original-file JSON search is separate.`;
}

let statsRequest=0;
async function loadStats() {
  const request=++statsRequest;
  const statsParams=new URLSearchParams(); if(state.datasetId)statsParams.set("dataset_id",state.datasetId);
  const data = await api(`/api/stats${statsParams.size?`?${statsParams}`:""}`); if(request!==statsRequest)return; state.stats = data;
  state.datasetId=data.active_dataset_id==null?"":String(data.active_dataset_id);
  state.sources=data.sources||[];
  const t=data.totals;
  $("#statMessages").textContent=compactNumber(t.messages);
  $("#statEvents").textContent=compactNumber(t.unique_events);
  $("#statConversations").textContent=compactNumber(t.conversations);
  $("#statAnomalies").textContent=compactNumber(t.anomalies);
  $("#datasetCount").textContent=data.datasets.length;
  $("#datasetSelect").innerHTML=data.datasets.map(d=>`<option value="${d.id}" ${String(d.id)===state.datasetId?'selected':''}>DATASET ${d.id} · ${d.file_count} FILES · ${d.source_count} LEDGER</option>`).join("");
  $("#datasetSelect").disabled=!data.datasets.length;
  const activeDataset=data.datasets.find(d=>String(d.id)===state.datasetId);
  const fileScope=activeDataset&&activeDataset.file_count!==activeDataset.source_count?` · ${activeDataset.file_count-activeDataset.source_count} raw-only file${activeDataset.file_count-activeDataset.source_count===1?'':'s'}`:'';
  const crossScope=t.events_seen_elsewhere?` · ${t.events_seen_elsewhere.toLocaleString()} also retained elsewhere${t.conflicting_events_elsewhere?` · ${t.conflicting_events_elsewhere.toLocaleString()} text conflicts`:''}${t.variant_events_elsewhere?` · ${t.variant_events_elsewhere.toLocaleString()} non-text variants`:''}${t.omitted_events_elsewhere?` · ${t.omitted_events_elsewhere.toLocaleString()} availability variants`:''}`:'';
  $("#datasetScope").textContent=(t.duplicate_event_copies?`${t.messages.toLocaleString()} records · ${t.unique_events.toLocaleString()} unique · ${t.duplicate_event_copies.toLocaleString()} retained copies`:`${t.messages.toLocaleString()} records · no repeated nodes`)+fileScope+crossScope;
  $("#sourceCount").textContent=data.sources.length;
  $("#anyMarkerCount").textContent=compactNumber(t.anomalies);
  $("#sourceList").innerHTML=data.sources.map(s=>{const displayStatus=s.status==='skipped'?'raw only':s.status;return `<button class="source-card ${String(s.id)===state.sourceId?'active':''}" data-source="${s.id}" title="${escapeHtml(s.path)}"><strong>${escapeHtml(fileName(s.path))}</strong><small>${compactNumber(s.row_count)} ledger rows · ${fmtBytes(s.size_bytes)}</small><span class="source-status ${escapeHtml(s.status)}">${escapeHtml(displayStatus)}</span>${s.error?`<small class="source-error">${escapeHtml(s.error.replace('No supported conversation or chronological records found','Available in JSON view; no conversation-shaped records found'))}</small>`:''}${s.duplicate_copies?`<small class="copy-note">${s.duplicate_copies} identical source cop${s.duplicate_copies===1?'y':'ies'} retained elsewhere</small>`:''}</button>`}).join("") || `<div class="source-card"><strong>NO SOURCE LOADED</strong><small>Use LOAD SOURCE to begin</small></div>`;
  $("#roleFilters").innerHTML=data.roles.map(r=>`<button class="filter-row ${state.role===r.value?'active':''}" data-role="${escapeHtml(r.value)}"><i></i><span>${escapeHtml(r.value || 'unknown').toUpperCase()}</span><b>${compactNumber(r.count)}</b></button>`).join("");
  $("#anomalyFilters").innerHTML=data.rules.map(r=>`<button class="filter-row ${state.anomaly===r.rule?'active':''}" data-anomaly="${escapeHtml(r.rule)}" title="${escapeHtml(r.title)}"><i class="severity-${r.severity}"></i><span>${escapeHtml(r.rule.replaceAll('_',' '))}</span><b>${compactNumber(r.count)}</b></button>`).join("");
  $(".all-markers").classList.toggle("active",state.anomaly==="any");
  bindDynamicFilters();
  updateImportProgress(data.job);
}

function bindDynamicFilters() {
  $$('[data-role]').forEach(el=>el.onclick=()=>{ state.role=state.role===el.dataset.role?"":el.dataset.role; state.page=1; loadStats(); loadMessages(); });
  $$('[data-anomaly]').forEach(el=>el.onclick=()=>{ state.anomaly=state.anomaly===el.dataset.anomaly?"":el.dataset.anomaly; state.page=1; loadStats(); loadMessages(); });
  $$('[data-source]').forEach(el=>el.onclick=async()=>{
    state.sourceId=state.sourceId===el.dataset.source?"":el.dataset.source;
    state.page=1; state.json.offset=0; state.json.match=0;
    await loadStats();
    if(state.view==='json')loadJsonSource();else if(state.view==='lineage'&&window.traceglassAtlas)window.traceglassAtlas.atlas(true);else loadMessages();
  });
}

function showLoadingRows() {
  $("#messageRows").innerHTML=Array.from({length:8},()=>`<tr class="loading-row"><td></td><td><div class="skeleton"></div></td><td><div class="skeleton"></div></td><td><div class="skeleton"></div></td><td></td><td></td><td></td></tr>`).join("");
}

let messageRequest=0;
async function loadMessages() {
  const request=++messageRequest;
  showLoadingRows();
  const params=new URLSearchParams({page:state.page,page_size:state.pageSize,search_mode:state.searchMode});
  if(state.query)params.set("q",state.query); if(state.role)params.set("role",state.role);
  if(state.anomaly)params.set("anomaly",state.anomaly); if(state.sourceId)params.set("source_id",state.sourceId);
  if(state.datasetId)params.set("dataset_id",state.datasetId);
  if(state.exact){params.set('meta_path',state.exact.path);params.set('meta_value',state.exact.value)}
  try {
    const data=await api(`/api/messages?${params}`); if(request!==messageRequest)return; state.total=data.total; state.pages=data.pages;state.page=data.page;
    state.searchScope=data.search_scope;showSearchScope(data.search_scope);
    renderRows(data.items); renderPager();
  } catch(err) { if(request!==messageRequest)return; $("#messageRows").innerHTML=""; toast(err.message); }
}

function renderRows(items) {
  const body=$("#messageRows"); $("#emptyState").hidden=items.length!==0;
  const hasData=(state.stats?.totals?.messages || 0)>0;
  $("#emptyState h2").textContent=hasData?'No matching records':'No indexed records';
  $("#emptyState p").textContent=hasData?'Nothing matched in this dataset with the current search and filters. Clear filters or inspect another dataset.':'Load a local export to begin.';
  $("#emptyImportButton").hidden=hasData;
  body.innerHTML=items.map(item=>{
    const t=fmtTime(item.create_time), role=escapeHtml(item.role || "unknown"), marked=item.anomaly_count>0;
    const hiddenBadges=`${item.visually_hidden?'<em class="evidence-badge visual-hidden">HIDDEN FROM CHAT</em>':item.visually_hidden_present?'<em class="evidence-badge hidden-meta">VISUAL-HIDDEN FIELD</em>':item.hidden_metadata?'<em class="evidence-badge hidden-meta">HIDDEN META</em>':''}`;
    return `<tr data-pk="${item.pk}" class="${state.selected?.pk===item.pk?'selected':''}">
      <td class="marker-cell"><span class="marker-count ${marked?'':'clear'}"><span>${marked?item.anomaly_count:'·'}</span></span></td>
      <td><span class="time-primary">${escapeHtml(t.main)}</span><span class="time-secondary">${escapeHtml(t.sub)}</span></td>
      <td><span class="role-badge role-${role}">${role}</span></td>
      <td class="content-cell"><strong>${escapeHtml(item.title || 'Untitled')}${hiddenBadges}</strong>${item.match_path?`<small class="match-path" title="${escapeHtml(item.match_path)}">${escapeHtml(item.match_path)}${item.matching_field_count>1?` · ${item.matching_field_count} matching fields`:''}</small>`:''}<span>${highlightedLiteral(item.content_preview || `[${item.content_type || 'empty'}]`,state.exact?'':state.query.replace(/^meta:/i,'').replace(/^"|"$/g,''))}</span></td>
      <td class="mono-cell">${escapeHtml(item.model_slug || '—')}</td>
      <td><span class="lineage-mini"><code>${escapeHtml(shortId(item.parent_id))}</code><i></i><code>${escapeHtml(shortId(item.node_id))}</code></span></td>
      <td><span class="review-pill ${item.review_state}">${escapeHtml(item.review_state)}</span></td>
    </tr>`;
  }).join("");
  $$('tr[data-pk]',body).forEach(row=>row.onclick=()=>openDetail(Number(row.dataset.pk)));
}

function renderPager() {
  const start=state.total?(state.page-1)*state.pageSize+1:0, end=Math.min(state.page*state.pageSize,state.total);
  $("#resultRange").textContent=`${start.toLocaleString()}–${end.toLocaleString()}`; $("#resultTotal").textContent=`of ${state.total.toLocaleString()}`;
  $("#currentPage").value=state.page; $("#totalPages").textContent=state.pages;
  $("#prevPage").disabled=state.page<=1; $("#nextPage").disabled=state.page>=state.pages;
}

function literalField(obj,key) {
  if(!obj || !Object.prototype.hasOwnProperty.call(obj,key))return 'ABSENT';
  return obj[key]===null?'NULL':typeof obj[key]==='string'?obj[key]:JSON.stringify(obj[key]);
}

function fieldsMarkup(item) {
  const metadataParent=item.metadata?.parent_id;
  const fields=[["MESSAGE ID",item.message_id],["OUTER parent",literalField(item.raw,'parent')],["METADATA parent_id",literalField(item.metadata,'parent_id')],["CHILDREN",(item.children||[]).join(', ')||'none'],["REQUEST ID",item.request_id],["TURN EXCHANGE",item.turn_exchange_id],["RAW CREATE TIME",item.raw_create_time],["TIME STATUS",item.timestamp_status],["CONTENT TYPE",item.content_type],["STATUS",item.status],["RECIPIENT",item.recipient],["MODEL",item.model_slug],["FLATTENED HASH",item.content_hash],["VISIBLE TEXT HASH",item.visible_text_hash],["CANONICAL CONTENT HASH",item.canonical_content_hash]];
  return fields.map(([k,v])=>`<div><small>${k}</small><code title="${escapeHtml(v||'')}">${escapeHtml(v??'—')}</code></div>`).join("");
}

function metadataMarkup(item) {
  const hits=item.hidden_metadata_hits||[];
  const groups=[[`HIDDEN METADATA OCCURRENCES · ${hits.length}`,hits],["MESSAGE METADATA",item.metadata],["SOURCE LOCATOR",item.source_locator],["GRAPH NODE (SOURCE KEYS)",{id:item.node_id,...(Object.prototype.hasOwnProperty.call(item.raw,'parent')?{parent:item.raw.parent}:{}),children:item.children}],["SOURCE PROVENANCE",{path:item.source_path,sha256:item.source_sha256,source_id:item.source_id,dataset_id:item.source_dataset_id}],[`OTHER DATASET APPEARANCES · ${(item.other_appearances||[]).length}`,item.other_appearances||[]]];
  return groups.map(([name,value],index)=>`<div class="meta-group ${index===0&&hits.length?'priority-meta':''}"><button type="button"><span>${name}</span><b>−</b></button><pre>${escapeHtml(JSON.stringify(value,null,2))}</pre></div>`).join("");
}

function parentChecksMarkup(checks) {
  const reasons={missing_field:'Stopped: this field is absent. That is not a root declaration.',null_parent:'Stopped: this field explicitly contains null.',target_not_in_source:'Stopped: target not found in this source file and conversation. It may exist in another chunk or export.',cycle:'Stopped: a node repeats (cycle).',invalid_parent_value:'Stopped: parent is not a nonempty string ID.',limit:`Stopped at the ${checks.limit}-node inspection limit; chain may continue.`};
  const comparison={equal:'The two parent values match on this record.',different:'The two parent values differ on this record.',metadata_parent_missing:'metadata.parent_id is absent on this record; no value was substituted.',outer_parent_missing:'The outer parent field is absent on this record.'};
  return `<p class="trace-note">${escapeHtml(comparison[checks.comparison])}</p><p class="trace-note">Same source file and conversation only. No links borrowed from other exports. These are field-lookup tests, not a reconstruction of the live model’s context.</p>`+
    [[checks.outer,'Follow outer parent'],[checks.metadata,'If a reader follows only metadata.parent_id']].map(([trace,label])=>`<article class="trace-walk"><h3>${escapeHtml(label)}</h3><p class="trace-stop">${escapeHtml(reasons[trace.stop.reason])}<br><code>${escapeHtml(trace.stop.node_id)}</code></p>${trace.issues.map(issue=>`<p class="trace-warning">Parent ${escapeHtml(issue.parent)} does not list child ${escapeHtml(issue.node_id)}.</p>`).join('')}<details><summary>Inspect ${trace.steps.length} nodes · ${trace.field==='parent'?`${trace.issues.length} backlink conflicts`:'backlinks not tested on this hypothetical walk'}</summary><ol>${trace.steps.map(step=>`<li><button class="trace-node" data-trace-pk="${step.pk}"><strong>${escapeHtml(step.role||'empty node')}${step.hidden?' · hidden':''}</strong><code>${escapeHtml(step.node_id)}</code></button><small>Field: ${escapeHtml(step.field_state)}${step.parent_lists_child===true?' · parent lists this child':step.parent_lists_child===false?' · BACKLINK MISSING':''}</small></li>`).join('')}</ol></details></article>`).join('');
}

function parentVersionsMarkup(item) {
  const scopes={same_source:'Target in same source',other_file_same_dataset:'Target in another file of this dataset',absent_in_dataset:'Target absent from this imported dataset',not_a_reference:'Not an ID reference'};
  const fieldMarkup=field=>`<code>${escapeHtml(field.state==='value'?field.value:field.state.toUpperCase())}</code><small>${escapeHtml(scopes[field.target_scope])}</small>${field.targets.map(t=>`<button class="trace-node" data-trace-pk="${t.pk}">${escapeHtml(t.role||'empty node')}${t.author_name?' / '+escapeHtml(t.author_name):''} · source ${t.source_id}<small>${t.lists_child?'Lists this node as child':'Does not list this node as child'}</small></button>`).join('')}${field.targets_truncated?'<small>Target copies limited to 50.</small>':''}`;
  return `<p class="trace-note">Same conversation ID + node ID across imported sources. Copies are not independent events. Targets are checked within each copy’s dataset, never borrowed from another export.</p><div class="version-table-wrap"><table class="version-table"><thead><tr><th>Source copy</th><th>Outer parent</th><th>Metadata parent_id</th></tr></thead><tbody>${item.parent_versions.items.map(v=>`<tr class="${v.pk===item.pk?'current-version':''}"><td><button class="trace-node" data-trace-pk="${v.pk}">Inspect dataset ${v.dataset_id} · source ${v.source_id}${v.pk===item.pk?' · selected':''}</button><details><summary>${escapeHtml(fileName(v.source_path))}</summary><code>${escapeHtml(v.source_path)}</code></details></td><td>${fieldMarkup(v.outer)}</td><td>${fieldMarkup(v.metadata_parent)}</td></tr>`).join('')}</tbody></table></div>${item.parent_versions.truncated?'<p class="trace-warning">Only the first 50 source copies are shown.</p>':''}<h3 class="trace-note">Parent-key spelling check (selected record only)</h3><p class="trace-note">Checks direct node and message.metadata keys. Similar names are candidates, not confirmed typos.</p>${item.parent_key_candidates.length?`<pre class="raw-record">${escapeHtml(JSON.stringify(item.parent_key_candidates,null,2))}</pre>`:'<p class="trace-note">No near-spelling parent keys found in these two locations.</p>'}`;
}

function markerMarkup(items) {
  if(!items.length)return `<div class="no-markers">No deterministic structural marker fired for this record.</div>`;
  return '<p class="trace-note">These markers are generated by Traceglass checks. They are not runtime flags emitted by ChatGPT; severity describes this app’s review priority.</p>'+items.map(m=>`<article class="marker-card ${m.severity}"><header><strong>${escapeHtml(m.title)}</strong><em>${escapeHtml(m.severity.toUpperCase())} · ${escapeHtml(m.rule)}</em></header><code>${escapeHtml(JSON.stringify(m.evidence,null,2))}</code></article>`).join("");
}

async function openDetail(pk) {
  try {
    const item=await api(`/api/messages/${pk}`); state.selected=item;
    $("#detailKicker").textContent=`${(item.role||'unknown').toUpperCase()} · ${fmtTime(item.create_time).main} · DATASET ${item.source_dataset_id} / SOURCE ${item.source_id}`;
    $("#detailTitle").textContent=item.title || item.node_id;
    const metadataParent=item.metadata?.parent_id;
    $("#identityStrip").innerHTML=`<div><small>NODE</small><code title="${escapeHtml(item.node_id)}">${escapeHtml(item.node_id)}</code></div><div><small>OUTER parent</small><code title="${escapeHtml(literalField(item.raw,'parent'))}">${escapeHtml(literalField(item.raw,'parent'))}</code></div><div class="${metadataParent&&metadataParent!==item.parent_id?'identity-conflict':''}"><small>METADATA parent_id</small><code title="${escapeHtml(literalField(item.metadata,'parent_id'))}">${escapeHtml(literalField(item.metadata,'parent_id'))}</code></div><div><small>SOURCE</small><code title="${escapeHtml(item.source_path)}">${escapeHtml(fileName(item.source_path))}</code></div>`;
    $("#messageContent").textContent=item.content_text || `[No display text · ${item.content_type || 'unknown content type'}]`;
    $("#messageFields").innerHTML=fieldsMarkup(item); $("#metadataTree").innerHTML=metadataMarkup(item);
    $("#parentChecks").innerHTML=parentChecksMarkup(item.parent_checks);
    $("#parentVersions").innerHTML=parentVersionsMarkup(item);
    $$('#detailPanel [data-trace-pk]').forEach(button=>button.onclick=()=>openDetail(Number(button.dataset.tracePk)));
    $("#markerList").innerHTML=markerMarkup(item.anomalies); $("#markerTabCount").textContent=item.anomalies.length;
    $("#rawRecord").textContent=JSON.stringify(item.raw,null,2); $("#reviewState").value=item.review_state;
    $("#reviewTags").value=item.tags; $("#reviewNote").value=item.note;
    $("#detailPanel").classList.add("open"); $("#detailPanel").setAttribute("aria-hidden","false");
    $$('tr[data-pk]').forEach(r=>r.classList.toggle('selected',Number(r.dataset.pk)===pk));
    $$('.meta-group>button').forEach(b=>b.onclick=()=>{const g=b.parentElement;g.classList.toggle('collapsed');b.querySelector('b').textContent=g.classList.contains('collapsed')?'+':'−';});
  } catch(err){toast(err.message)}
}

async function saveAnnotation() {
  if(!state.selected)return;
  try { await api(`/api/messages/${state.selected.pk}/annotation`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({review_state:$("#reviewState").value,tags:$("#reviewTags").value,note:$("#reviewNote").value})}); toast("Review layer saved locally"); await loadMessages(); }
  catch(err){toast(err.message)}
}

function setView(view) {
  const search=$("#searchInput");
  if(state.view==='json')state.json.query=search.value.trim();else state.query=search.value.trim();
  state.view=view; $$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-selected',b.dataset.view===view)});
  document.body.classList.toggle('lineage-mode',view==='lineage');
  $("#ledgerView").classList.toggle("active",view==="ledger"); $("#lineageView").classList.toggle("active",view==="lineage"); $("#jsonView").classList.toggle("active",view==="json");
  search.value=view==='json'?state.json.query:state.query;
  $('#searchMode').disabled=view!=='ledger';
  $('#searchScopeNote').textContent=view==='json'?'Literal search in the selected original file; display loads a bounded byte window.':view==='lineage'?'Graph search covers loaded nodes only; atlas search covers conversation titles and IDs.':'';
  if(view==='ledger')showSearchScope(state.searchScope);
  search.placeholder=view==='json'?'Search exact serialized file (e.g. hidden)…':'Search text, exact phrase, node ID, or meta:hidden…';
  if(view==="lineage")loadLineage();
  if(view==="json"){
    closeDetail();
    if(!state.sourceId&&state.sources.length)state.sourceId=String(state.sources[0].id);
    loadJsonSource();
  } else if(view==='ledger'){
    renderPager();
  }
}

async function loadJsonSource() {
  if(!state.sourceId){
    $("#jsonFileName").textContent='Select any source file';
    $("#jsonRawViewer").textContent='Choose a JSON-family file in SOURCES.';
    return;
  }
  const params=new URLSearchParams({offset:state.json.offset,length:state.json.length});
  if(state.json.query){params.set('q',state.json.query);params.set('match',state.json.match)}
  try{
    const data=await api(`/api/sources/${state.sourceId}/raw?${params}`);state.json.data=data;state.json.offset=data.offset;state.json.match=data.match_index;
    $("#jsonFileName").textContent=fileName(data.source.path);
    $("#jsonFilePath").textContent=data.source.path;
    $("#jsonSourceStatus").textContent=`${data.source.status==='skipped'?'RAW ONLY':data.source.status.toUpperCase()} · ${fmtBytes(data.source.size_bytes)}`;
    $("#jsonByteRange").textContent=`${data.offset.toLocaleString()}–${data.end_offset.toLocaleString()}${data.total_bytes!=null?` / ${data.total_bytes.toLocaleString()}`:''}`;
    $("#jsonMatchStatus").textContent=data.query?(data.match_count?`${data.match_index+1} / ${data.match_count}${data.match_limit_reached?'+':''}`:'0 / 0'):'NO QUERY';
    $("#jsonRawViewer").innerHTML=highlightedLiteral(data.content,data.query);
    const sourceNote=data.source.error?.replace('No supported conversation or chronological records found','Raw source available; no conversation-shaped ledger records found');
    $("#jsonScopeNote").innerHTML=`${escapeHtml(data.scope_note)}${sourceNote?` <code>${escapeHtml(sourceNote)}</code>`:''}`;
    $("#jsonPrevChunk").disabled=data.offset<=0;$("#jsonNextChunk").disabled=data.eof;
    $("#jsonPrevMatch").disabled=!data.query||data.match_index<=0;$("#jsonNextMatch").disabled=!data.query||data.match_index>=data.match_count-1;
    $("#resultRange").textContent=`${data.offset.toLocaleString()}–${data.end_offset.toLocaleString()}`;$("#resultTotal").textContent=data.total_bytes!=null?`of ${data.total_bytes.toLocaleString()} bytes`:'bytes (streamed)';
    $("#jsonRawViewer").scrollTop=0;
    if(data.query)requestAnimationFrame(()=>$("#jsonRawViewer mark")?.scrollIntoView({block:'center'}));
  }catch(err){toast(err.message)}
}

async function loadLineage() {
  if(window.traceglassAtlas){window.traceglassAtlas.open(state.selected);return;}
  if(!state.selected)return;
  const p=new URLSearchParams({source_id:state.selected.source_id,conversation_id:state.selected.conversation_id,focus:state.selected.node_id,depth:7});
  try { const data=await api(`/api/lineage?${p}`); $("#lineageTitle").textContent=state.selected.title; renderGraph(data); }
  catch(err){toast(err.message)}
}

function renderGraph(data) {
  const empty=$("#graphEmpty"); empty.hidden=data.nodes.length>0;
  const byId=new Map(data.nodes.map(n=>[n.id,n])); const kids=new Map();
  data.nodes.forEach(n=>{if(n.parent&&byId.has(n.parent)){if(!kids.has(n.parent))kids.set(n.parent,[]);kids.get(n.parent).push(n.id)}});
  const roots=data.nodes.filter(n=>!n.parent||!byId.has(n.parent)); const pos=new Map(); let leaf=0;
  function place(id,depth,seen=new Set()){if(seen.has(id)){const y=105+leaf++*108;pos.set(id,{x:120+depth*220,y});return y}seen=new Set(seen);seen.add(id);const cs=(kids.get(id)||[]).filter(c=>!pos.has(c));const x=120+depth*220;if(!cs.length){const y=105+leaf++*108;pos.set(id,{x,y});return y}const ys=cs.map(c=>place(c,depth+1,seen));const y=ys.reduce((a,b)=>a+b,0)/ys.length;pos.set(id,{x,y});return y}
  roots.forEach(r=>place(r.id,0));
  data.nodes.filter(n=>!pos.has(n.id)).forEach(n=>{pos.set(n.id,{x:120,y:105+leaf++*108})});
  const maxX=Math.max(1100,...[...pos.values()].map(p=>p.x+195)),maxY=Math.max(540,...[...pos.values()].map(p=>p.y+120)); $("#lineageGraph").setAttribute("viewBox",`0 0 ${maxX} ${maxY}`);
  $("#graphEdges").innerHTML=data.edges.map(e=>{const a=pos.get(e.source),b=pos.get(e.target);if(!a||!b)return'';const x1=a.x+84,x2=b.x-84,mid=(x1+x2)/2;const kinds=e.kinds||['active_graph'];const cls=kinds.includes('metadata_parent')&&kinds.includes('other_export_graph')?'corroborated-edge':kinds.includes('metadata_parent')?'metadata-edge':kinds.includes('other_export_graph')?'other-edge':'active-edge';const label=`${kinds.join(' + ')} · dataset${(e.dataset_ids||[]).length===1?'':'s'} ${(e.dataset_ids||[]).join(', ')}`;return `<path class="graph-edge ${cls}" d="M${x1},${a.y} C${mid},${a.y} ${mid},${b.y} ${x2},${b.y}"><title>${escapeHtml(label)}</title></path>`}).join('');
  $("#graphNodes").innerHTML=data.nodes.map(n=>{const p=pos.get(n.id)||{x:0,y:0},color=n.role==='user'?'var(--acid)':n.role==='assistant'?'var(--violet)':'var(--cyan)',focus=n.id===state.selected?.node_id;const flags=[n.hidden?'HIDDEN FROM CHAT':(n.hidden_metadata?'HIDDEN META':''),n.supplemental?'OTHER EXPORT':'',n.metadata_parent_corroborated?'PARENT CORROBORATED':''].filter(Boolean);const datasets=(n.dataset_ids||[]).map(d=>`D${d}`).join('·');return `<g class="graph-node ${focus?'focus':''} ${n.hidden?'hidden-node':''} ${n.hidden_metadata?'hidden-metadata-node':''} ${n.supplemental?'supplemental-node':''}" data-pk="${n.pk}" transform="translate(${p.x-84},${p.y-34})"><rect width="168" height="68" rx="2"/><rect class="node-accent" width="3" height="68" fill="${color}"/><text x="12" y="18">${escapeHtml((n.role||'unknown').toUpperCase())} · ${escapeHtml(shortId(n.id,8))}</text><text class="node-sub" x="12" y="37">${escapeHtml(shortId(n.preview||'[empty]',23))}</text><text class="node-flags" x="12" y="55">${escapeHtml(flags.join(' · ')||datasets)}${flags.length&&datasets?` · ${escapeHtml(datasets)}`:''}</text>${n.anomalies?`<circle class="node-marker" cx="155" cy="12" r="4"/>`:''}</g>`}).join('');
  $$('.graph-node').forEach(n=>{
    // The SVG canvas captures pointer events for panning. Keep a node press from
    // being retargeted to the canvas so every node, including supplemental
    // cross-export nodes, remains directly inspectable.
    n.onpointerdown=e=>e.stopPropagation();
    n.onclick=e=>{e.stopPropagation();openDetail(Number(n.dataset.pk))};
  });
  resetGraph(); if(data.truncated)toast("Graph window capped at 700 nodes; focus a node to move through it");
}

function applyGraphTransform(){const g=state.graph;$("#graphViewport").setAttribute("transform",`translate(${g.x} ${g.y}) scale(${g.scale})`)}
function resetGraph(){state.graph={...state.graph,scale:1,x:0,y:0};applyGraphTransform()}
function zoomGraph(factor){state.graph.scale=Math.max(.3,Math.min(3,state.graph.scale*factor));applyGraphTransform()}

async function beginImport(path, sample=false) {
  try {
    const res=await api(sample?"/api/import/sample":"/api/import",{method:"POST",headers:{"Content-Type":"application/json"},body:sample?"{}":JSON.stringify({paths:[path]})});
    $("#importProgress").hidden=false; toast(res.started?"Indexing started":"Import unavailable"); pollJob();
  } catch(err){toast(err.message)}
}

async function importSelectedFile() {
  const file=$('#importFile').files[0], button=$('#importFileButton');
  if(!file){toast('Choose a JSON export first');return}
  if(!file.size || file.size>20*1024**3){toast('Choose a nonempty file up to 20 GiB, or use Local export path');return}
  button.disabled=true;button.textContent='COPYING…';
  try {
    // Send the File directly: do not build a second full-file string in memory.
    const saved=await api(`/api/upload?name=${encodeURIComponent(file.name)}`,{
      method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file
    });
    $('#importPath').value=saved.path;
    await beginImport(saved.path);
  } catch(err){toast(err.message)}
  finally {button.disabled=false;button.textContent='LOAD ↗'}
}

let jobTimer;
async function pollJob(){clearTimeout(jobTimer);try{const p=state.datasetId?`?dataset_id=${encodeURIComponent(state.datasetId)}`:'';const data=await api(`/api/stats${p}`);updateImportProgress(data.job);if(data.job.state==='running')jobTimer=setTimeout(pollJob,700);else{if(data.job.state==='complete')state.datasetId='';await loadStats();await loadMessages();if(data.job.state==='complete')toast(data.job.detail)}}catch(err){toast(err.message)}}
function updateImportProgress(job){if(!job)return;const box=$("#importProgress");if(job.state==='idle'){box.hidden=true;return}box.hidden=false;const pct=job.total?Math.round(job.current/job.total*100):0;$("#importLabel").textContent=job.label||job.state;$("#importPercent").textContent=`${pct}%`;$("#importProgressBar").value=pct;$("#importDetail").textContent=job.error||job.detail||'';if(job.state==='running'){clearTimeout(jobTimer);jobTimer=setTimeout(pollJob,800)}}

function savedViews(){try{const v=JSON.parse(localStorage.getItem('traceglass.savedViews.v1')||'[]');return Array.isArray(v)?v:[]}catch{return []}}
function renderSavedViews(){const views=savedViews();$('#savedQueries').innerHTML='<option value="">Choose a saved view…</option>'+views.map((v,i)=>`<option value="${i}">${escapeHtml(v.name)}</option>`).join('')}
function syncExact(){const e=state.exact;$('#metaPath').value=e?.path||'';$('#metaType').value=e?.type||'text';$('#metaValue').value=e?.input||'';$('#exactStatus').textContent=e?' · EXACT FILTER ACTIVE':''}
function bindQueryTools(){
  renderSavedViews();
  $('#applyExact').onclick=()=>{try{const path=$('#metaPath').value.trim(),type=$('#metaType').value,input=$('#metaValue').value; if(!path)throw Error('Enter a metadata path');let value=input;if(type==='number'){if(!input.trim()||!Number.isFinite(Number(input)))throw Error('Enter a finite number');value=Number(input)}if(type==='boolean'){if(!['true','false'].includes(input))throw Error('Boolean value must be true or false');value=input==='true'}if(type==='null')value=null;state.exact={path,type,input,value:JSON.stringify(value)};syncExact();state.page=1;setView('ledger');loadMessages()}catch(e){toast(e.message)}};
  $('#clearExact').onclick=()=>{state.exact=null;syncExact();state.page=1;loadMessages()};
  $('#saveQuery').onclick=()=>{try{const name=$('#queryName').value.trim();if(!name)throw Error('Name this view before saving');const views=savedViews();if(views.length>=100)throw Error('Saved view limit reached (100)');views.push({name,query:state.query,searchMode:state.searchMode,page:state.page,pageSize:state.pageSize,role:state.role,anomaly:state.anomaly,sourceId:state.sourceId,datasetId:state.datasetId,exact:state.exact});localStorage.setItem('traceglass.savedViews.v1',JSON.stringify(views));renderSavedViews();$('#savedQueries').value=String(views.length-1);toast('Search footprint saved in this browser')}catch(e){toast(e.message)}};
  $('#openQuery').onclick=async()=>{try{const i=$('#savedQueries').value;if(i==='')return;const v=savedViews()[Number(i)];if(!v)return;if(!state.stats.datasets.some(d=>String(d.id)===String(v.datasetId)))throw Error('This saved dataset is no longer available');state.query=v.query||'';state.searchMode=['auto','text','ids','metadata'].includes(v.searchMode)?v.searchMode:'auto';state.role=v.role||'';state.anomaly=v.anomaly||'';state.sourceId=v.sourceId||'';state.datasetId=v.datasetId;state.exact=v.exact||null;state.page=Number.isSafeInteger(v.page)&&v.page>0?v.page:1;state.pageSize=[50,100,250].includes(v.pageSize)?v.pageSize:100;state.selected=null;closeDetail();$('#searchInput').value=state.query;$('#searchMode').value=state.searchMode;$('#pageSize').value=String(state.pageSize);syncExact();await loadStats();if(state.sourceId&&!state.sources.some(s=>String(s.id)===String(state.sourceId)))throw Error('This saved source is no longer available');setView('ledger');await loadMessages();toast('Search footprint restored')}catch(e){toast(e.message)}};
}
function bindEvents(){
  bindQueryTools();
  bindPageJump('ledgerPageJump','currentPage',()=>state.pages,page=>{state.page=page;loadMessages();});
  $('#searchMode').onchange=e=>{state.searchMode=e.target.value;state.page=1;loadMessages();};
  $("#openImportButton").onclick=$("#emptyImportButton").onclick=()=>$("#importDialog").showModal();
  $("#importSampleButton").onclick=()=>beginImport('',true); $("#importPathButton").onclick=()=>{const p=$("#importPath").value.trim();if(p)beginImport(p);else toast("Enter a local file or directory path")};
  $('#importFileButton').onclick=importSelectedFile;
  $("#prevPage").onclick=()=>{if(state.page>1){state.page--;loadMessages()}}; $("#nextPage").onclick=()=>{if(state.page<state.pages){state.page++;loadMessages()}};
  $("#pageSize").onchange=e=>{state.pageSize=Number(e.target.value);state.page=1;loadMessages()};
  $("#datasetSelect").onchange=async e=>{state.datasetId=e.target.value;state.sourceId='';state.page=1;state.selected=null;state.json.offset=0;state.json.match=0;closeDetail();await loadStats();if(state.view==='json'){if(state.sources.length)state.sourceId=String(state.sources[0].id);loadJsonSource()}else loadMessages()};
  $("#clearRole").onclick=()=>{state.role='';state.page=1;loadStats();loadMessages()}; $("#clearAnomaly").onclick=()=>{state.anomaly='';state.page=1;loadStats();loadMessages()};
  let searchTimer; $("#searchInput").oninput=e=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{if(state.view==='json'){state.json.query=e.target.value.trim();state.json.match=0;state.json.offset=0;loadJsonSource()}else{state.query=e.target.value.trim();state.page=1;loadMessages()}},250)};
  document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();if(state.view==='lineage'&&window.traceglassAtlas)window.traceglassAtlas.focusSearch();else $("#searchInput").focus()}if(e.key==='Escape'&&$("#detailPanel").classList.contains('open'))closeDetail()});
  $("#timeModeButton").onclick=()=>{state.timeMode=state.timeMode==='local'?'utc':'local';$("#timeModeButton").textContent=state.timeMode==='local'?'LOCAL TIME':'UTC TIME';loadMessages();window.traceglassAtlas?.refreshTime();if(state.selected&&$("#detailPanel").classList.contains('open'))openDetail(state.selected.pk)};
  $$('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view)); $("#closeDetail").onclick=closeDetail; $("#saveAnnotation").onclick=saveAnnotation;
  $$('.detail-tabs button').forEach(b=>b.onclick=()=>{$$('.detail-tabs button').forEach(x=>x.classList.toggle('active',x===b));$$('.tab-pane').forEach(p=>p.classList.toggle('active',p.id===`tab-${b.dataset.tab}`))});
  $("#descendLineage").onclick=()=>{setView('lineage');closeDetail();loadLineage()}; $("#resetGraphButton").onclick=resetGraph; $("#zoomIn").onclick=()=>zoomGraph(1.2); $("#zoomOut").onclick=()=>zoomGraph(.82);
  $("#jsonPrevChunk").onclick=()=>{state.json.offset=Math.max(0,state.json.offset-state.json.length);state.json.query='';$("#searchInput").value='';loadJsonSource()};
  $("#jsonNextChunk").onclick=()=>{state.json.offset=(state.json.data?.end_offset||0);state.json.query='';$("#searchInput").value='';loadJsonSource()};
  $("#jsonPrevMatch").onclick=()=>{state.json.match=Math.max(0,state.json.match-1);loadJsonSource()};
  $("#jsonNextMatch").onclick=()=>{state.json.match=Math.min((state.json.data?.match_count||1)-1,state.json.match+1);loadJsonSource()};
  const svg=$("#lineageGraph");svg.addEventListener('wheel',e=>{e.preventDefault();zoomGraph(e.deltaY<0?1.08:.92)},{passive:false});svg.addEventListener('pointerdown',e=>{state.graph.dragging=true;state.graph.sx=e.clientX-state.graph.x;state.graph.sy=e.clientY-state.graph.y;svg.setPointerCapture(e.pointerId)});svg.addEventListener('pointermove',e=>{if(state.graph.dragging){state.graph.x=e.clientX-state.graph.sx;state.graph.y=e.clientY-state.graph.sy;applyGraphTransform()}});svg.addEventListener('pointerup',()=>state.graph.dragging=false);
  $("#homeButton").onclick=()=>{state.page=1;state.query=state.role=state.anomaly=state.sourceId='';state.exact=null;syncExact();state.json={...state.json,query:'',offset:0,match:0,data:null};$("#searchInput").value='';closeDetail();setView('ledger');loadStats();loadMessages()};
}
function closeDetail(){ $("#detailPanel").classList.remove("open"); $("#detailPanel").setAttribute("aria-hidden","true"); }

async function init(){bindEvents();try{await loadStats();await loadMessages()}catch(err){toast(`Traceglass could not start: ${err.message}`)}}
init();
