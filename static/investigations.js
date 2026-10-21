/* Local journal UI. Deliberately no provider, voice, shell, or autonomous calls. */
(() => {
  const panel = $('#investigationDialog');
  const journal = {id:null, data:null, cases:[], before:null, busy:false, revises:null, drafts:new Map()};
  const labels = {inspect_record:'Record inspection', trace_parents:'Parent trace', compare_exports:'Export comparison'};
  const stopLabels={null_parent:'explicit null parent',missing_field:'parent field absent',limit:'inspection limit reached; chain may continue',cycle:'cycle encountered',target_not_in_source:'target absent from this source',invalid_parent_value:'invalid parent value'};
  const post = (path,body) => api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const endpoint = tail => `/api/investigations/${journal.id}${tail||''}`;
  const error = message => {$('#investigationError').textContent=message||'';$('#investigationError').hidden=!message;};
  const time = value => new Date(value).toLocaleString();
  function selection() {
    const record=state.selected;
    $('#investigationSelection').textContent=record ? `${record.title||'Untitled'} · source ${record.source_id} · ${record.node_id}` : 'Open a record in the ledger to attach evidence.';
    $$('.investigation-actions button').forEach(b=>b.disabled=journal.busy||!record||!journal.id);
  }
  async function task(action) {
    if(journal.busy)return;
    journal.busy=true;error('');selection();
    $$('button, input, select, textarea',panel).forEach(el=>{el.dataset.wasDisabled=String(el.disabled);el.disabled=true;});
    try{await action();}catch(e){error(e.message);}
    finally{
      journal.busy=false;
      $$('[data-was-disabled]',panel).forEach(el=>{el.disabled=el.dataset.wasDisabled==='true';delete el.dataset.wasDisabled;});
      selection();
    }
  }
  function saveDraft(){if(journal.id)journal.drafts.set(journal.id,{text:$('#investigationText').value,kind:$('#investigationKind').value,revises:journal.revises});}
  function restoreDraft(){const d=journal.drafts.get(journal.id)||{text:'',kind:'note',revises:null};$('#investigationText').value=d.text;$('#investigationKind').value=d.kind;journal.revises=d.revises;revisionLabel();}
  function revisionLabel(){$('#revisionStatus').textContent=journal.revises?`Revising entry #${journal.revises}`:'';$('#cancelRevision').hidden=!journal.revises;}
  function renderList(){
    $('#investigationList').innerHTML=journal.cases.map(c=>`<button class="investigation-item ${c.id===journal.id?'active':''}" data-case-id="${escapeHtml(c.id)}"><strong>${escapeHtml(c.title)}</strong><small>${escapeHtml(time(c.created_at))}</small></button>`).join('');
    $('#moreInvestigations').hidden=!journal.before;
  }
  async function cases(older=false){
    const data=await api('/api/investigations'+(older?`?before=${journal.before}`:''));
    journal.cases=older?[...journal.cases,...data.items]:data.items;journal.before=data.before;renderList();
  }
  function resultSummary(payload){
    const r=payload.result;
    if(payload.tool==='trace_parents')return `Outer: ${r.outer.steps.length} nodes · ${stopLabels[r.outer.stop.reason]||r.outer.stop.reason}. Metadata: ${r.metadata.steps.length} nodes · ${stopLabels[r.metadata.stop.reason]||r.metadata.stop.reason}. Export structure only; this is not a runtime diagnosis.`;
    if(payload.tool==='compare_exports')return `${r.items.length} imported ${r.items.length===1?'copy':'copies'}${r.truncated?' (comparison capped)':''}. Same conversation and node IDs; copies are not independent events.`;
    return `${r.role||'No role'} · ${r.content_type||'No content type'} · model ${r.model_slug||'not recorded'}. Open the pinned record for its full text and metadata.`;
  }
  function eventMarkup(e){
    const p=e.payload||{},ref=p.record;
    let body=`<p>${escapeHtml(e.kind==='tool_request'?(labels[e.text]||e.text):e.text)}</p>`;
    if(e.kind==='tool_result'){
      body=`<p>${escapeHtml(resultSummary(p))}</p>`;
      if(p.tool==='trace_parents')body+=['outer','metadata'].map(field=>`<details><summary>Walk ${field==='outer'?'outer parent':'metadata parent_id'} · ${p.result[field].issues.length} recorded issues</summary><ol class="case-walk">${p.result[field].steps.map((s,i)=>`<li><button data-walk-event="${e.id}" data-walk-field="${field}" data-walk-index="${i}">${escapeHtml(s.role||'no message')} · ${escapeHtml(s.node_id)}${s.hidden?' · HIDDEN':''}</button></li>`).join('')}</ol></details>`).join('');
      body+=`<details><summary>Expand recorded result</summary><pre>${escapeHtml(JSON.stringify(p.result,null,2))}</pre></details>`;
    }
    const evidence=ref?`<div class="case-record"><span>SOURCE ${ref.source_id} · ${escapeHtml(ref.node_id)}</span><button data-open-record="${e.id}">OPEN RECORD ↗</button><details><summary>Source locator</summary><pre>${escapeHtml(JSON.stringify(ref,null,2))}</pre></details></div>`:'';
    const editable=e.actor==='user'&&['note','question','hypothesis','correction','finding','next_step'].includes(e.kind);
    return `<article class="case-event case-${escapeHtml(e.kind)}"><header><small>${e.actor==='local_tool'?'LOCAL TOOL · RECORDED OUTPUT':'YOU · '+escapeHtml(e.kind.replaceAll('_',' ')).toUpperCase()}${e.kind==='hypothesis'?' · UNVERIFIED':''}${e.kind==='finding'?' · YOUR ASSESSMENT':''}${p.revises?` · REVISION OF #${p.revises}`:''}</small><time>${escapeHtml(time(e.created_at))}</time></header>${body}${evidence}<footer><span>#${e.id}</span>${editable?`<button data-revise-entry="${e.id}">REVISE</button>`:''}</footer></article>`;
  }
  function renderJournal(){
    const d=journal.data;
    $('#activeInvestigationTitle').textContent=d.investigation.title;
    $('#activeInvestigationQuestion').textContent=d.investigation.question;
    $('#investigationEntries').innerHTML=d.events.map(eventMarkup).join('');
    $('#olderInvestigationEntries').hidden=!d.before;
    $('#createInvestigation').hidden=true;$('#investigationWorkspace').hidden=false;
    renderList();selection();
  }
  async function openCase(id){
    const data=await api(`/api/investigations/${id}`);
    saveDraft();journal.id=id;journal.data=data;restoreDraft();renderJournal();
    $('#investigationJournal').scrollTop=$('#investigationJournal').scrollHeight;
  }
  async function refresh(){journal.data=await api(endpoint());renderJournal();$('#investigationJournal').scrollTop=$('#investigationJournal').scrollHeight;}
  async function openRecord(ref){
    // Resolve the saved identity again instead of trusting a potentially recycled pk.
    const record=await api(`/api/messages/${ref.pk}`);
    if(record.source_id!==ref.source_id||record.node_id!==ref.node_id||record.conversation_id!==ref.conversation_id||record.source_sha256!==ref.source_sha256)throw Error('This index record changed. Saved provenance is preserved; re-locate the source before continuing.');
    panel.close();await openDetail(ref.pk);toast('Evidence opened. INVESTIGATE returns to your notebook.');
  }
  $('#openInvestigation').onclick=()=>{panel.showModal();selection();task(async()=>{await cases();if(!journal.id&&journal.cases.length)await openCase(journal.cases[0].id);});};
  $('#closeInvestigation').onclick=()=>{saveDraft();panel.close();};
  panel.addEventListener('cancel',e=>{if(journal.busy)e.preventDefault();else saveDraft();});
  $('#newInvestigation').onclick=()=>{saveDraft();$('#createInvestigation').hidden=false;$('#investigationWorkspace').hidden=true;error('');$('#investigationName').focus();};
  $('#createInvestigation').onsubmit=e=>{e.preventDefault();task(async()=>{
    const c=await post('/api/investigations',{title:$('#investigationName').value,question:$('#investigationQuestion').value});
    await cases();await openCase(c.id);$('#createInvestigation').reset();
  });};
  $('#investigationList').onclick=e=>{const b=e.target.closest('[data-case-id]');if(b)task(()=>openCase(b.dataset.caseId));};
  $('#moreInvestigations').onclick=()=>task(()=>cases(true));
  $('#olderInvestigationEntries').onclick=()=>task(async()=>{
    const el=$('#investigationJournal'),height=el.scrollHeight;
    const d=await api(endpoint(`?before=${journal.data.before}`));
    journal.data.events=[...d.events,...journal.data.events];journal.data.before=d.before;renderJournal();el.scrollTop=el.scrollHeight-height;
  });
  $('#investigationComposer').onsubmit=e=>{e.preventDefault();task(async()=>{
    const body={kind:$('#investigationKind').value,text:$('#investigationText').value};
    if(journal.revises)body.revises=journal.revises;
    await post(endpoint('/events'),body);journal.drafts.delete(journal.id);restoreDraft();await refresh();
  });};
  $('#cancelRevision').onclick=()=>{journal.revises=null;revisionLabel();};
  $$('.investigation-actions button').forEach(b=>b.onclick=()=>task(async()=>{
    if(!state.selected)throw Error('Select a record first');
    if(b.dataset.caseTool){const r=await post(endpoint('/tools'),{tool:b.dataset.caseTool,pk:state.selected.pk});await refresh();if(!r.ok)throw Error(r.error);}
    else{const kind=b.dataset.caseAction;await post(endpoint('/events'),{kind,pk:state.selected.pk,text:kind==='checkpoint'?'Return point saved. This does not change the original question.':'Pinned for review; pinning is not a factual conclusion.'});await refresh();}
  }));
  $('#investigationEntries').onclick=e=>{
    if(journal.busy)return;
    const open=e.target.closest('[data-open-record]'),revise=e.target.closest('[data-revise-entry]'),walk=e.target.closest('[data-walk-event]');
    if(walk){const entry=journal.data.events.find(x=>x.id===Number(walk.dataset.walkEvent));const step=entry.payload.result[walk.dataset.walkField].steps[Number(walk.dataset.walkIndex)];task(()=>openRecord({...entry.payload.record,pk:step.pk,node_id:step.node_id}));}
    if(open){const entry=journal.data.events.find(x=>x.id===Number(open.dataset.openRecord));task(()=>openRecord(entry.payload.record));}
    if(revise){
      const entry=journal.data.events.find(x=>x.id===Number(revise.dataset.reviseEntry));
      if($('#investigationText').value.trim()&&!confirm('Replace the unsaved draft with this entry?'))return;
      journal.revises=entry.id;$('#investigationKind').value=entry.kind;$('#investigationText').value=entry.text;revisionLabel();$('#investigationText').focus();
    }
  };
})();
