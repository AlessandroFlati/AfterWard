'use strict';
const app = document.querySelector('#app');
const path = location.pathname.replace(/\/$/, '');
const role = path.endsWith('/staff') ? 'staff' : path.endsWith('/patient') ? 'patient' : null;
const base = role ? path.replace(/\/(staff|patient)$/, '') : path;
let state = null, config = null, busy = false, drafts = {}, inviteLink = '', activeRead = null, activeUpload = null, mobileMenuOpen = false;
const joinCode = () => location.hash.startsWith('#join=') ? location.hash.slice(6) : null;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const url = (area, page='') => `${base}/${area}${page ? '#' + page : ''}`;
const stamp = value => value ? new Date(value).toLocaleString('en-GB', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : '';
const personal = () => config.personal;
const page = () => location.hash.slice(1) || (role === 'staff' ? 'requests' : 'documents');
const waiting = q => ['open','needs_review'].includes(q.status);
const staffQuestions = (resolved=false) => state.questions.filter(q=>(q.status==='resolved')===resolved).sort((a,b)=>{
  const priority=q=>q.active_reminders.length?0:waiting(q)?1:q.status==='answered'?2:3;
  return priority(a)-priority(b);
});
const statusLabel = q => ({open:personal()?'Ready to contact':'Waiting for the ward',needs_review:'Needs review',answered:personal()?'Reply recorded':role==='staff'?'Waiting for the patient':'Reply available',resolved:role==='staff'?'Resolved by the patient':'Resolved by you'}[q.status] || q.status);
const storageKey = () => `afterward-thread-forms-${state.csrf}-${role}`;
function dismissNotice() {
  clearTimeout(notice.timer);document.querySelector('#notice').hidden=true;document.querySelector('#notice-message').textContent='';
}
function notice(text) {
  text=friendlyMessage(text);
  const box=document.querySelector('#notice');document.querySelector('#notice-message').textContent=text;box.hidden=false;
  clearTimeout(notice.timer);notice.timer=setTimeout(()=>{if(!box.contains(document.activeElement))dismissNotice();},10000);
}
function remember() {
  app.querySelectorAll('[data-persist]').forEach(el=>drafts[el.dataset.persist]=el.tagName==='DETAILS'?el.open:el.type==='checkbox'?el.checked:el.value);
  const questionFields=app.querySelectorAll('#ask-form textarea[name="question"]');
  if(questionFields.length)drafts.question_fields=[...questionFields].map(el=>el.value);
  if(state&&!personal())sessionStorage.setItem(storageKey(),JSON.stringify(drafts));
}
function restore() {
  app.querySelectorAll('[data-persist]').forEach(el=>{if(Object.hasOwn(drafts,el.dataset.persist)){if(el.tagName==='DETAILS')el.open=drafts[el.dataset.persist];else if(el.type==='checkbox')el.checked=drafts[el.dataset.persist];else el.value=drafts[el.dataset.persist];}});
  updateSelection();
}
function confirmAction(title,body,submit='Confirm') {
  const dialog=document.querySelector('#confirm');document.querySelector('#confirm-title').textContent=title;
  document.querySelector('#confirm-body').innerHTML=body;document.querySelector('#confirm-submit').textContent=submit;
  dialog.returnValue='';dialog.showModal();document.querySelector('#confirm-title').focus();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
function friendlyMessage(text) {
  if(/HTTP 429|too many requests|service is busy/i.test(text))return 'The suggestion service is busy. Wait a moment, then try again. You can also write and send your reply directly.';
  if(/quota|AMD worker|inference.*progress/i.test(text))return 'Checking is unavailable right now. You can still read your documents and prepare questions.';
  if(/timeout|timed out|finish within/i.test(text))return 'AfterWard could not finish in time. Your documents are saved. Try again.';
  if(/\b(?:AMD|Gemma|API|JSON|inference|fingerprint|source_id)\b/i.test(text))return 'AfterWard could not complete this task. Your documents are saved. Try again or prepare a question for the ward.';
  return text;
}
function errorMessage(value) {
  return ({'The episode changed; refresh before continuing.':'This case changed in another tab. Select Refresh; your draft will be kept.',
    'A reminder was already recorded; wait until the displayed next time.':'One selected question already has a reminder in the last 24 hours. Update your selection.',
    'Access denied.':'This action is unavailable for your access.',
    'Documents changed. Review and save the draft again.':'Documents changed. Review and save your draft again before sending.',
    'A question changed or has overlapping drafts. Review the selection.':'A question changed or has overlapping drafts. Refresh and review the replies.',
    'Open the latest reply before asking for clarification.':'Open the latest reply before asking for more details.',
    'Only the latest unanswered acknowledgement can be recorded.':'This reply has been superseded. Refresh to see the latest message.',
    'Text is empty or exceeds the supported length.':'Enter text within the stated limit.'})[value.detail]||value.detail||'The action failed. Refresh and try again.';
}
async function request(endpoint,body) {
  const headers={'X-AfterWard-Request':'1'};if(body){headers['Content-Type']='application/json';if(state)headers['X-AfterWard-CSRF']=state.csrf;}
  const response=await fetch(`${base}/api/${endpoint}`,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
  const value=await response.json();if(!response.ok)throw new Error(errorMessage(value));return value;
}
async function mutate(action,payload,success,target,clear=[]) {
  if(busy)return;remember();busy=true;app.setAttribute('aria-busy','true');
  try{state=await request(`${role}/${action}`,{revision:state.revision,...payload});clear.forEach(key=>delete drafts[key]);
    render();if(target&&page()!==target)location.hash=target;notice(success);return true;
  }catch(error){notice(error.message);return false;}finally{busy=false;app.removeAttribute('aria-busy');}
}
const chip=q=>`<span class="chip ${q.status==='resolved'?'good':waiting(q)?'open':'progress'}">${esc(statusLabel(q))}</span>`;
const empty=text=>`<div class="empty">${text}</div>`;
function breadcrumbs(title,parent='requests',label=role==='staff'?'Open questions':'My questions') {
  return `<nav class="breadcrumbs" aria-label="Breadcrumbs"><a href="#${parent}">${label}</a><span aria-hidden="true">/</span><span>${esc(title)}</span></nav>`;
}
function landing() {
  if(joinCode()&&!personal())return `<main class="landing"><a class="brand" href="${base}/">After<span>Ward</span></a><h1>Open the shared case</h1><p>Join the case of the person who sent this link. Questions, replies and PDFs will be shared across your devices.</p><p>This link expires in 10 minutes and works once. It will replace the current demo session in this browser.</p><button class="primary" data-action="join">Open the case</button></main>`;
  if(personal())return `<main class="landing" id="main"><a class="brand" href="${base}/">After<span>Ward</span></a><div class="eyebrow">Personal workspace on your computer</div>
    <h1>Start with your discharge letter.</h1><p class="lede">Upload a PDF, compare the transcription with each page and prepare questions for a ward that has not adopted AfterWard.</p>
    <div class="card"><h2>What you can try</h2><p>Read PDFs, prepare linked questions, export them and record replies received outside the app.</p>
    <p>Documents stay on this computer. You can review their text, prepare questions and record replies. Automatic document checks are unavailable in this workspace, and questions are not sent to a ward.</p>
    <p class="muted">Up to 5 pages and 12 MiB per PDF. Sessions last 12 hours; you can delete them in Session settings. Use a private, protected computer for personal documents.</p>
    ${state?`<a class="button primary" href="${url('patient','documents')}">Resume your personal session</a><p class="muted">To start another case, delete this session in Session settings first.</p>`:'<button class="primary" data-start="patient">Open personal workspace</button>'}</div></main>`;
  return `<main class="landing" id="main"><a class="brand" href="${base}/">After<span>Ward</span></a><div class="eyebrow">Interactive demo</div>
    <h1>One discharge.<br>A conversation that continues.</h1><p class="lede">Understand the letter, find information to clarify, and ask only what still needs the ward. Each AI suggestion links back to your documents.</p>
    <div class="actions"><a class="button primary" href="${base}/judge-guide">Try the guided cases</a><a class="button" href="${base}/presentation">Video and presentation walkthrough</a></div>
    <p class="notice-inline">Explore with example documents and identities. AI suggests; people review and choose what to send. No external messages are sent. ${['127.0.0.1','localhost'].includes(location.hostname) ? 'For a personal PDF, use the <a href="http://127.0.0.1:8777/">separate local workspace</a>.' : 'Only invented, non-sensitive documents may be used in this demo.'}</p>
    <div class="options"><section class="card"><h2>The patient has their documents</h2><p>Check the letter, explain medical wording and look for existing answers before sending a question.</p><button class="primary" data-start="hospital">Start as a patient</button></section>
    <section class="card"><h2>The ward prepares discharge</h2><p>Check the documents already in the case file, review suggestions and share the completed bundle.</p><button data-start="before_delivery">Start as ward staff</button></section>
    <section class="card"><h2>The patient brings a PDF</h2><p>Read a scanned letter, compare it with the original and check the reviewed text.</p><button data-start="patient">Start with a PDF</button></section></div>
    <p><a href="${url('patient')}">Resume patient view</a> · <a href="${url('staff')}">Resume ward view</a></p></main>`;
}
const navLink=(hash,text,active)=>`<a href="#${hash}" ${active?'aria-current="page"':''}>${hash==='documents'?'<span class="nav-full-label">Documents</span><span class="nav-short-label" aria-hidden="true">Docs</span>':text}</a>`;
function actionItems() {
  if(role!=='staff'||!state)return [];
  const questions=staffQuestions().filter(waiting).map(q=>({href:'thread-'+q.id,title:q.text,
    reason:q.active_reminders.length?'Reminder received':state.replies.some(r=>!r.delivered&&r.covered.includes(q.id))?'Draft to review and send':q.messages.length>1?'Patient follow-up':'Needs a reply',
    action:'Open conversation'}));
  return questions;
}
function updateNotifications() {
  const shortcut=document.querySelector('#activity-shortcut');
  const count=actionItems().length;
  shortcut.hidden=role!=='staff'||!state||page()==='activity'||!count;
  if(!shortcut.hidden)shortcut.querySelector('.activity-count').textContent=String(count);
}
function activityView() {
  const items=actionItems();
  return `<h1>Action required</h1><p>Open an item to reply or review it. Items leave this list when the required action is complete.</p><div class="actions"><a class="button" href="#requests">Open questions</a><a class="button" href="#resolved">Resolved questions</a></div>
    ${items.length?`<ul class="activity-list">${items.map(item=>`<li><a href="#${item.href}"><span class="activity-kind">${esc(item.reason)}</span><strong class="question-text">${esc(item.title)}</strong><span>${item.action} &rarr;</span></a></li>`).join('')}</ul>`:empty('Nothing needs a ward response right now. Questions awaiting patient confirmation remain in Open questions.')}`;
}
function shell(content) {
  const current=page(), requests=['requests','status','answers','questions','resolved','activity','reply-batch'].includes(current)||current.startsWith('thread-'), aiPage=['ai','check','explain'].includes(current)||current.startsWith('explain-')||current.startsWith('question-check-');
  const showingResolved=current==='resolved'||current.startsWith('thread-')&&state.questions.some(q=>q.id===current.slice(7)&&q.status==='resolved');
  const top=`<header class="appbar"><div class="brand-block"><a class="brand" href="${base}/">After<span>Ward</span></a><span class="mobile-context">${esc(state.person)}${role==='staff'?' · Ward view':''}</span></div><nav aria-label="Main navigation">
    ${navLink(role==='staff'?'requests':'documents',role==='staff'?'Questions':'Documents',role==='staff'?requests&&current!=='activity':!requests&&!aiPage&&current!=='settings')}
    ${role==='staff'?navLink('documents','Documents',['documents','add'].includes(current)||current.startsWith('reader-')||current.startsWith('upload-')):navLink('requests','My questions',requests)}
    ${navLink('ai','AI help',aiPage)}${role==='staff'?`<a class="nav-actions" href="#activity" ${current==='activity'?'aria-current="page"':''}>Actions${actionItems().length?` <span class="nav-count">${actionItems().length}</span>`:''}</a>`:''}${navLink('settings','Session',current==='settings')}</nav><button class="mobile-menu" data-action="toggle-menu" aria-expanded="${mobileMenuOpen}" aria-controls="account-tools">${mobileMenuOpen?'Close':'Options'}</button><div class="account ${mobileMenuOpen?'is-open':''}" id="account-tools"><span>${esc(personal()?'Personal workspace':role==='staff'?'Marta Ricci · Ward staff':state.person)}</span><button data-action="refresh">Refresh</button><a class="button" href="${base}/judge-guide" target="_blank" rel="noopener">Guide</a>
    ${!personal()?`<a class="button" href="${url(role==='staff'?'patient':'staff','requests')}">View ${role==='staff'?'patient':'ward'}</a>`:''}</div></header>${!personal()?`<div class="collaboration-bar"><span>Case ${esc(state.workspace_code||'condivisa')} · automatically updated</span><a href="#share">Connect another device</a></div>`:''}`;
  if(role!=='staff')return `${top}<main class="patient-shell" id="main">${personal()?'<p class="local-status">Documents on your computer · Ward not connected</p>':''}${content}</main>`;
  return `${top}<div class="staff-shell"><aside class="queue"><div class="eyebrow">Current case</div><h2>${esc(state.person)}</h2><p class="mono">${esc(state.episode_id)}</p>
    <nav class="queue-filters" aria-label="Conversation lists"><a class="button wide" href="#requests" ${!showingResolved?'aria-current="page"':''}>Open questions (${staffQuestions().length})</a><a class="button wide" href="#resolved" ${showingResolved?'aria-current="page"':''}>Resolved questions (${staffQuestions(true).length})</a></nav>
    <nav aria-label="Conversations">${staffQuestions(showingResolved).map(q=>`<a class="card ${current==='thread-'+q.id?'selected':''}" href="#thread-${q.id}"><strong class="question-text">${esc(q.text)}</strong><br>${chip(q)}</a>`).join('')}</nav>
    <nav class="queue-tools" aria-label="Case tools"><a class="button wide" href="#ai">AI help</a><a class="button wide" href="#documents">Open documents</a></nav></aside><main class="workspace" id="main">${content}</main></div>`;
}
function aiTools() {
  if(!config.ai_enabled)return '';
  return `<div class="ai-tools" aria-label="AI assistance"><a class="card request-link" href="#check"><strong>${role==='staff'?'Check before sharing':'Check for missing information'}</strong><p>Find missing details and conflicting instructions, with source passages.</p><span>Check documents &rarr;</span></a><a class="card request-link" href="#explain"><strong>Understand medical wording</strong><p>Read an AI explanation beside the original passage. Ask the ward if more information is needed.</p><span>Explain a passage &rarr;</span></a>${role==='patient'?'<a class="card request-link" href="#questions"><strong>Is the answer already here?</strong><p>Look in your documents before deciding to contact the ward.</p><span>Check a question &rarr;</span></a>':'<a class="card request-link" href="#requests"><strong>Help with a reply</strong><p>Open a conversation for an optional draft based on the case documents.</p><span>Open conversations &rarr;</span></a>'}</div>`;
}
function aiView() {
  const jobs=state.ai_jobs.filter(j=>['analyze','answer','explain'].includes(j.kind)).slice(-6).reverse();
  return `<h1>${role==='staff'?'AI support for discharge':'Understand your discharge'}</h1><p class="lede">${role==='staff'?'Check the documents, prepare clearer explanations and reply with the sources at hand.':'Find what is already written, make medical wording easier to understand and spot information to clarify.'}</p>${config.ai_enabled?aiTools():empty('AI is unavailable in this workspace. You can still read documents and prepare questions.')}
    ${jobs.length?`<section class="card"><h2>Recent AI activity</h2><ul class="activity-list">${jobs.map(j=>`<li><a href="#${j.kind==='answer'?'question-check-'+j.id:j.kind==='explain'?'explain-'+j.payload.documents[0]:'check'}"><strong>${j.kind==='answer'?'Questions checked against your documents':j.kind==='explain'?'Plain-language explanation':'Document review'}</strong><span>${j.stale?'Documents changed: check again':j.status==='completed'?j.kind==='explain'?'Open explanation and review status':'Results ready':j.status==='failed'?'Check failed: open for details':'In progress'}</span></a></li>`).join('')}</ul></section>`:''}`;
}
function explainView(id) {
  const doc=state.documents.find(d=>d.identifier===id);
  return `${breadcrumbs('Plain-language explanations','ai','AI help')}<h1>Understand medical wording</h1>${doc?`<p>Source: <a href="#reader-${esc(doc.identifier)}">${esc(doc.title)}</a></p>${explanationView(doc)}<a class="button" href="#explain">Choose another document</a>`:`<p>Choose a document, then a passage to explain. The original wording stays beside the explanation.</p><div class="stack">${state.documents.map(d=>`<a class="card request-link" href="#explain-${esc(d.identifier)}"><strong>${esc(d.title)}</strong><span>Choose a passage &rarr;</span></a>`).join('')||empty('Add a document first.')}<a class="button" href="#add">Upload a PDF</a></div>`}`;
}
function documentsView() {
  return `<h1>${role==='staff'?'Discharge documents':'Your documents'}</h1><p class="lede">${role==='staff'?'Review the case and choose what to share with the patient.':'Open a document or add a PDF. Find questions and replies under My questions.'}</p>
    ${aiTools()}<div class="actions"><a class="button" href="#add">Upload a PDF</a><a class="button" href="#requests">${role==='staff'?'Open questions':'Open questions and replies'}</a></div>
    ${state.uploads.filter(u=>!u.confirmed).map(u=>`<a class="card request-link open" href="#upload-${u.id}"><strong>${esc(u.title)}</strong><span>${activeUpload===u.id?'Reading your document...':config.ocr_provider==='amd_api'&&u.pages.some(p=>!p.gemma)?'Continue reading this PDF':'Review the pages to finish adding this document'}</span></a>`).join('')}
    <div class="stack">${state.documents.length?state.documents.map(d=>`<section class="card"><div class="row"><h2><a href="#reader-${d.identifier}">${esc(d.title)}</a></h2><span class="chip">${d.origin==='personal'?'Added by you':d.delivered?'Available to the patient':'In the ward case file'}</span></div><p>${d.demo_pdf||d.upload_id?'PDF document · original and transcription available':esc(d.text.slice(0,180))}</p><a class="button" href="#reader-${d.identifier}">Open document</a>
      ${role==='staff'&&d.approved?`<label class="check"><input type="checkbox" name="document" value="${d.identifier}" data-persist="doc-${d.identifier}">Include when sharing</label>`:''}</section>`).join(''):empty('No documents yet. Upload your first PDF.')}</div>
    ${role==='staff'?'<div class="batchbar"><p>Documents are shared separately from replies to questions.</p><button data-action="share-documents">Share selected documents</button></div>':''}`;
}
function readerView(id) {
  const doc=state.documents.find(d=>d.identifier===id);if(!doc)return breadcrumbs('Document','documents','Documents')+empty('Document unavailable.');
  const upload=state.uploads.find(u=>u.id===doc.upload_id);
  const pdfBase=upload?`${base}/api/${role}/uploads/${upload.id}`:doc.demo_pdf?`${base}/api/${role}/documents/${doc.identifier}`:null;
  return `${breadcrumbs(doc.title,'documents','Documents')}<h1>${esc(doc.title)}</h1><div class="actions"><a class="button" href="#documents">Back to documents</a>${pdfBase?`<a class="button" href="${pdfBase}/0" target="_blank" rel="noopener">Open original PDF</a>`:''}${config.ai_enabled?`<a class="button primary" href="#explain-${esc(doc.identifier)}">Explain medical wording</a><a class="button" href="#check">Check documents</a>`:''}${role!=='staff'?'<a class="button" href="#questions">Ask about this document</a>':'<a class="button" href="#requests">Back to questions</a>'}</div>
    ${upload?upload.pages.map(p=>`<section class="card"><h2>Page ${p.number}</h2><div class="pdf-columns"><a href="${pdfBase}/${p.number}" target="_blank" rel="noopener"><img class="pdf-page" src="${pdfBase}/${p.number}" alt="Original, page ${p.number}"></a><div><p class="muted">Text reviewed by the person who added it</p><div class="quote">${esc(p.text)}</div></div></div>${readingComparison(upload,p)}</section>`).join(''):doc.demo_pdf?`<article class="card"><a href="${pdfBase}/0" target="_blank" rel="noopener"><img class="pdf-page demo-original" src="${pdfBase}/1" alt="${esc(doc.title)}: original PDF page"></a><details><summary>Show document text</summary><div class="quote">${esc(doc.text)}</div></details></article>`:`<article class="card"><div class="quote">${esc(doc.text)}</div></article>`}
    `;
}
function jobStatus(job) {
  if(!job)return '';
  if(['queued','running'].includes(job.status))return loading(job.status==='queued'?'getting your check ready':job.kind==='explain'?'explaining this passage':job.kind==='answer'?'looking for answers in your documents':job.kind==='draft'?'preparing a reply suggestion':'checking your documents');
  if(job.status==='failed')return `<p class="notice-inline" role="alert">${esc(friendlyMessage(job.message))} Your documents remain available.</p>`;
  if(job.stale)return '<p class="notice-inline">The documents or conversation changed. Run a new check before using this result.</p>';
  return '';
}
function loading(task) {
  return `<div class="loading-state" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><span>AfterWard is ${esc(task)}...</span></div>`;
}
function glossaryView(doc) {
  if(!state.glossary_enabled)return '';
  const cached=state.glossaries?.[doc.identifier];
  if(!cached||['queued','running','interrupted'].includes(cached.status))return loading('clarifying medical wording');
  if(cached.status==='failed')return `<section class="card"><h2>Medical wording</h2><p>${esc(cached.message)}</p><button data-action="retry-glossary" data-document="${esc(doc.identifier)}">Try preparing explanations again</button></section>`;
  const terms=cached.result.terms;
  return `<section class="card glossary"><h2>Medical wording, explained</h2><p>${terms.length?'Choose a term to read its explanation.':'No unfamiliar clinical terms were identified. You can still select a passage below.'}</p><div class="glossary-list">${terms.map((item,index)=>`<details class="glossary-term"><summary>${esc(item.term)}</summary><div class="glossary-definition">${item.needs_clarification?'<p>This wording has more than one possible meaning here. The ward can clarify it.</p>':`<p class="plain-language">${esc(item.plain_language)}</p>`}<p class="caps">In your document</p><blockquote>${esc(item.source_quote)}</blockquote>${role!=='staff'?`<button data-action="ask-glossary" data-document="${esc(doc.identifier)}" data-index="${index}">${item.needs_clarification?'Prepare a question for the ward':'Still unclear? Ask the ward'}</button>`:''}</div></details>`).join('')}</div><p class="muted">AI explanations describe the wording and may contain errors. Keep the original instructions.</p></section>`;
}
function checkView() {
  const job=state.ai_jobs.filter(j=>j.kind==='analyze').at(-1);
  const findings=job?.status==='completed'&&!job.stale?job.result.findings:null;
  return `${breadcrumbs('Document check','documents','Documents')}<h1>${role==='staff'?'Check before discharge':'Check your documents'}</h1><p class="lede">Find points that may need clarification, with the original passages beside each suggestion.</p>
    <form id="check-form" class="card"><h2>Which documents should be checked together?</h2><p>${role==='staff'?'These are the documents in the ward case file, including those not yet shared.':'Only documents available to you are included. If you already have a paper copy of a referenced sheet, add it before checking again.'}</p>
    ${state.documents.map(d=>`<label class="check"><input type="checkbox" name="check-document" value="${esc(d.identifier)}" checked data-persist="check-doc-${d.identifier}">${esc(d.title)}</label>`).join('')}
    <p class="muted">${role==='staff'?'Review the suggestions against the sources before sharing documents.':'Review the suggestions before deciding to contact the ward.'}</p><button class="primary" ${!config.ai_enabled||state.ai_pending||!state.documents.length?'disabled':''}>${job?'Run a new check':'Check selected documents'}</button>${state.ai_pending?'<p>A check is already running for this case.</p>':''}</form>
    ${jobStatus(job)}${findings?`<section class="card"><h2>${findings.length?`${findings.length} point${findings.length===1?'':'s'} to review`:'No clarification points identified'}</h2><p class="muted">Checked: ${job.payload.documents.map(id=>esc(state.documents.find(d=>d.identifier===id)?.title||id)).join(', ')}.</p><p>${findings.length?'These are suggestions based on the checked documents. A referenced sheet may already be in your possession.':'The model found no supported gaps in this run. This does not establish clinical completeness or correctness.'}</p>
    ${findings.map(f=>`<article class="finding"><h3>${esc(f.label)}</h3>${f.citations.map(c=>`<a href="#reader-${esc(c.document_id)}">${esc(c.title)} - page ${c.document_page}</a><blockquote>${esc(c.quote)}</blockquote>`).join('')}<p>${esc(f.question)}</p>${role!=='staff'?`<label class="check"><input type="checkbox" name="finding" value="${esc(f.key)}" data-persist="finding-${job.id}-${esc(f.key)}" ${(job.asked_keys||[]).includes(f.key)?'disabled':''}>${(job.asked_keys||[]).includes(f.key)?'Already added to My questions':'Include this question'}</label>`:'<p class="muted">Review the source and update or add the relevant document before sharing. This check does not send anything to the patient.</p>'}</article>`).join('')}
    ${role!=='staff'&&findings.length?`<div class="actions"><button class="primary" data-action="ask-findings" data-job="${job.id}">Send selected questions together</button><a class="button" href="#add">I have another sheet to add</a></div>`:''}</section>`:''}<a class="button" href="#documents">Back to documents</a>`;
}
function explanationView(doc) {
  const job=state.ai_jobs.filter(j=>j.kind==='explain'&&j.payload.documents.includes(doc.identifier)).at(-1);
  const explanations=state.explanations.filter(e=>e.document_id===doc.identifier);
  const results=explanations.slice().reverse().map(e=>`<article class="card explanation-result"><h2>${e.needs_clarification?'This passage needs clarification':e.approved?'Explanation reviewed by the ward':'AI explanation'}</h2><p class="caps">Original passage</p><blockquote class="quote">${esc(e.source_quote)}</blockquote>${e.needs_clarification?'<p>The document does not give enough context for a reliable explanation. You can prepare a question for the ward.</p>':`<p class="plain-language">${esc(e.plain_language)}</p><p class="muted">${e.approved?'The ward has reviewed this explanation.':'Generated by AI to explain the wording; it may contain errors. Keep the original instructions and ask the ward if anything remains unclear.'}</p>`}<div class="actions">${role!=='staff'?`<button data-action="ask-explanation" data-id="${e.id}">${e.needs_clarification?'Prepare a question for the ward':'Still unclear? Ask the ward'}</button>`:!e.approved&&!e.needs_clarification?`<button data-action="approve-explanation" data-id="${e.id}">Mark as reviewed (optional)</button>`:''}</div></article>`).join('');
  return `${jobStatus(job)}${glossaryView(doc)}${results}<details class="card custom-explanation" ${state.glossary_enabled?'':'open'}><summary>Explain a different passage</summary>${config.ai_enabled?`<form class="explain-form" data-document="${esc(doc.identifier)}"><p>Copy the exact wording you want to understand from the <a href="#reader-${esc(doc.identifier)}">document</a>.</p><label for="quote-${doc.identifier}">Passage to explain</label><textarea id="quote-${doc.identifier}" name="quote" maxlength="5000" data-persist="explain-${doc.identifier}" required placeholder="Paste a passage from your document"></textarea><div class="actions"><button class="primary" ${state.ai_pending?'disabled':''}>Explain this passage</button></div></form>`:'<p>AI explanations are disabled on this server.</p>'}</details>`;
}
function aiReply(q) {
  const job=state.ai_jobs.filter(j=>j.kind==='draft'&&j.payload.question_id===q.id).at(-1);
  return `${config.ai_enabled?`<details class="reply-ai-sources" data-persist="draft-sources-open-${q.id}"><summary>Documents for the reply suggestion</summary><p>Choose the documents relevant to this question.</p>${state.documents.map(d=>`<label class="check"><input type="checkbox" name="draft-document-${q.id}" value="${esc(d.identifier)}" checked data-persist="draft-source-${q.id}-${esc(d.identifier)}">${esc(d.title)}</label>`).join('')}</details>`:''}${jobStatus(job)}${config.ai_enabled?`<button type="button" data-action="draft-ai" data-question="${q.id}" ${job?.retry_after_utc?`data-retry-at="${esc(job.retry_after_utc)}"`:''} ${state.ai_pending?'disabled':''}>${job?.status==='failed'?'Try reply suggestion again':'Suggest a reply from the documents'}</button>`:''}${job?.status==='completed'&&!job.stale?`<section class="ai-suggestion"><strong>AI suggestion - review before use</strong><p>${esc(job.result.text)}</p>${job.result.needs_clarification?'<p class="muted">Staff need to supply or clarify information before answering fully.</p>':''}${job.result.citations.map(c=>`<details><summary>${esc(c.title)} - page ${c.document_page}</summary><blockquote>${esc(c.quote)}</blockquote></details>`).join('')}<button type="button" data-action="use-ai-draft" data-job="${job.id}" data-question="${q.id}">Use in my reply editor</button></section>`:''}`;
}
function uploadForm(questionId) {
  const question=role==='staff'&&state.questions.find(q=>q.id===questionId);
  const back=question?'thread-'+question.id:'documents', label=question?'Back to reply':'Back to documents';
  return `${breadcrumbs('Add PDF',back,question?'Conversation':'Documents')}<h1>Add your document</h1><p>PDFs may contain selectable text or scans. After extraction, you can review and correct every page.</p>${question?'<p>You will return to your reply after reviewing the pages. Your reply text is kept, and the document is shared only when you choose.</p>':''}
    <form id="upload-form" class="card" data-return-thread="${question?esc(question.id):''}"><label for="pdf-file">PDF to add (1 to 5 pages, maximum 12 MiB)</label><input id="pdf-file" name="pdf" type="file" accept="application/pdf,.pdf" required>
    <p>${config.ocr_provider==='amd_api'?'Use the supplied examples without personal or confidential data.':config.ocr_provider==='disabled'?'Text can be read from searchable PDFs. For a scan, enter and review the text yourself.':'The PDF stays on this computer.'}</p>
    ${config.packaged_uploads_only?`<p>This public demo accepts the <a href="${base}/judge-guide#ocr" target="_blank" rel="noopener">downloadable example PDFs</a> only. Other files cannot be uploaded here.</p>`:''}
    <div class="actions"><button class="primary" type="submit">Read the PDF</button><a class="button" href="#${back}">${label}</a></div><div id="upload-progress"></div></form>`;
}
function uploadThread(id) {
  const questionId=drafts['upload-thread-'+id];
  return role==='staff'&&state.questions.some(q=>q.id===questionId)?questionId:null;
}
function readingComparison(upload,p) {
  const g=p.gemma;
  if(activeRead===`${upload.id}-${p.number}`)return loading('reading this page');
  if(!g&&personal())return '<p class="muted">Compare the text with the original page and correct it where needed.</p>';
  if(!g&&config.ocr_provider!=='amd_api')return '';
  if(!g)return `<p>Reading is not complete. Your PDF is saved.</p><button type="button" data-action="read-page" data-upload="${upload.id}" data-page="${p.number}">Read page ${p.number} again</button>`;
  return `<section class="ocr-comparison" aria-label="Original transcription, page ${p.number}"><details><summary>Text before your corrections</summary><div class="quote transcription">${esc(g.text)}</div></details>${personal()&&p.original_text?`<details><summary>Earlier reading</summary><div class="quote transcription">${esc(p.original_text)}</div></details>`:''}</section>`;
}
function reviewUpload(id) {
  const u=state.uploads.find(item=>item.id===id);if(!u)return breadcrumbs('Review PDF')+empty('Document unavailable.');if(u.confirmed)return readerView('pdf-'+id);
  const returnThread=uploadThread(id);
  if(activeUpload===id||activeRead?.startsWith(id+'-'))return `${breadcrumbs('Reading PDF','documents','Documents')}<h1>${esc(u.title)}</h1>${loading('reading your document')}<a class="button" href="#documents">Back to documents</a>`;
  if(config.ocr_provider==='amd_api'&&u.pages.some(p=>!p.gemma))return `${breadcrumbs('Finish reading PDF','documents','Documents')}<h1>${esc(u.title)}</h1><section class="card"><h2>Reading is not finished</h2><p>Your PDF is saved. Continue reading it to review the complete text.</p><div class="actions"><button class="primary" data-action="read-upload" data-upload="${id}">Continue reading</button><a class="button" href="#documents">Back to documents</a></div></section>`;
  return `${breadcrumbs('PDF review','documents','Documents')}<h1>Review the pages of ${esc(u.title)}</h1><p>Compare the extracted text with each original page. Open a page to enlarge it. Correct numbers, names and reading order; do not fill in missing instructions.</p>
    <form id="confirm-upload" data-upload="${id}" class="stack">${u.pages.map(p=>`<section class="card"><h2>Page ${p.number}</h2>${p.warnings.length?`<details><summary>Reading notes</summary>${p.warnings.map(w=>`<p>${esc(w)}</p>`).join('')}</details>`:''}
      <div class="pdf-columns"><a href="${base}/api/${role}/uploads/${id}/${p.number}" target="_blank" rel="noopener"><img class="pdf-page" src="${base}/api/${role}/uploads/${id}/${p.number}" alt="Original page ${p.number}; open to enlarge"></a><div><label for="page-${p.number}">Text to review</label><textarea id="page-${p.number}" class="page-text" name="page" data-persist="upload-${id}-${p.number}" maxlength="24000" required>${esc(p.text)}</textarea></div></div><details><summary>Transcription details</summary>${readingComparison(u,p)}</details></section>`).join('')}
    <label class="check"><input type="checkbox" name="reviewed" required>I compared every page and checked that this document belongs to this case.</label><p class="muted">This confirms the transcription and selected case. It does not verify the author or clinical correctness.</p>
    <div class="actions"><button class="primary">${returnThread?'Accept text and return to reply':'Accept reviewed text'}</button><a class="button" href="#${returnThread?'thread-'+returnThread:'documents'}">Finish later</a></div></form>`;
}
function threadMessages(q) {
  const messages=q.messages.map(m=>({...m,kind:'patient'}));
  const replies=state.replies.filter(r=>r.delivered&&r.covered.includes(q.id)).map(r=>({...r,at_utc:r.delivered_at_utc,kind:'reply'}));
  return [...messages,...replies].sort((a,b)=>(a.at_utc||'').localeCompare(b.at_utc||''));
}
function conversationHistory(q) {
  return threadMessages(q).map(m=>`<div class="bubble ${m.kind}"><div class="row"><strong>${esc(m.kind==='patient'?(role==='staff'?m.author:'You'):m.author)}</strong><small>${stamp(m.at_utc)}</small></div>${m.reply_to?'<small>Follow-up in this conversation</small>':''}
    ${m.origin==='patient_recorded_external'?'<small>External reply recorded by you; AfterWard has not verified the sender.</small>':''}<p class="message-text">${esc(m.text)}</p>${m.kind==='reply'&&m.id!==q.answered_by?'<small>Previous reply</small>':''}</div>`).join('');
}
function replyDocuments(q) {
  const name='reply-document-'+q.id;
  const available=state.documents.filter(d=>d.approved&&!d.delivered);
  const pending=state.uploads.filter(u=>!u.confirmed&&uploadThread(u.id)===q.id);
  return `<section class="reply-documents" aria-label="Documents for this reply"><div class="actions"><a class="button" href="#add-for-${q.id}">Upload a PDF for this reply</a></div>${pending.map(u=>`<a class="card request-link" href="#upload-${u.id}"><strong>${esc(u.title)}</strong><span>Continue reviewing this document</span></a>`).join('')}<details data-persist="reply-documents-open-${q.id}"><summary>Share a document with the patient</summary><p>Sharing a document is optional and separate from sending your reply.</p>${available.length?available.map(d=>`<label class="check"><input type="checkbox" name="${name}" value="${esc(d.identifier)}" data-persist="${name}-${esc(d.identifier)}">${esc(d.title)}</label>`).join('')+`<button type="button" data-action="share-documents" data-selection="${name}">Share selected documents</button>`:'<p>All reviewed documents are already available to the patient.</p>'}${state.documents.some(d=>d.delivered)?`<p class="muted">Already shared: ${state.documents.filter(d=>d.delivered).map(d=>esc(d.title)).join(', ')}.</p>`:''}</details></section>`;
}
function staffEditor(q,batch=false) {
  if(!waiting(q))return `<p class="muted">${q.status==='resolved'?'The patient confirmed that their question is resolved.':'Reply sent. The patient decides whether their question is resolved.'}</p>`;
  const draft=state.replies.filter(r=>!r.delivered&&r.covered.length===1&&r.covered[0]===q.id).at(-1);
  return `<form class="reply-form" data-question="${q.id}" data-reply="${draft?.id||''}"><label for="reply-${q.id}">Reply to the ${q.messages.length>1?'latest follow-up':'patient'}</label><textarea id="reply-${q.id}" name="text" maxlength="10000" data-persist="reply-${q.id}" required placeholder="Write your reply directly. No additional documents are required.">${esc(draft?.text||'')}</textarea>
    <p class="muted">${batch?'Write the reply, then include it below. No separate save is needed to send.':`Sending makes this reply available to ${esc(state.person)}.`} Unsent text stays in this browser while you work.</p>
    <div class="actions">${batch?`<label class="check"><input type="checkbox" name="send-question" value="${q.id}" data-persist="batch-${q.id}">Include this reply</label>`:'<button type="submit" class="primary" value="send">Send reply</button>'}<button type="submit" value="save">Save for later</button></div>${replyDocuments(q)}${aiReply(q)}</form>`;
}
function patientThreadActions(q) {
  if(q.status==='answered'||q.status==='resolved')return `<div class="thread-outcome">${q.status==='resolved'?'<p class="ack">You confirmed this question is resolved. You can reopen the conversation if needed.</p>':`<div class="actions"><button class="primary" data-action="understood" data-question="${q.id}" data-reply="${q.answered_by}">I understand, question resolved</button></div>`}
    <details><summary>${q.status==='resolved'?'I have another question about this':'Still unclear: ask for more details'}</summary><form class="follow-form" data-question="${q.id}" data-reply="${q.answered_by}"><label for="follow-${q.id}">What would you like clarified?</label><textarea id="follow-${q.id}" name="text" maxlength="2000" data-persist="follow-${q.id}" required></textarea><button class="primary">${personal()?'Prepare follow-up':'Ask in this conversation'}</button></form></details></div>`;
  const option=state.reminder_action.options.find(o=>o.question_id===q.id);
  return `<p class="muted">${q.messages.length>1?'Your latest follow-up is above. The earlier reply stays in this conversation.':personal()?'Export this question and contact the ward through a channel you already know.':'This question is waiting for the ward.'}</p>
    <div class="actions"><button data-action="remind-one" data-question="${q.id}" ${option?.available?'':'disabled'}>${personal()?'Prepare a reminder':'Send a reminder for this question'}</button>${option&&!option.available?`<small>Next reminder available ${stamp(option.next_allowed_at_utc)}</small>`:''}</div>
    ${personal()?`<details><summary>Record a reply you received</summary><form class="external-form" data-question="${q.id}"><label>From whom and through which channel?<input name="source" maxlength="200" required placeholder="Example: outpatient clinic, phone call on..."></label><label>Reply received<textarea name="text" maxlength="10000" required data-persist="external-${q.id}"></textarea></label><button>Record in conversation</button></form></details>`:''}`;
}
function threadCard(q) {
  return `<article class="card thread-card" id="question-${q.id}"><div class="row"><p class="muted">${role==='staff'?`Conversation with ${esc(state.person)}`:'Your conversation with the ward'}</p>${chip(q)}</div>
    ${q.active_reminders.length?`<p class="reminder-meta">${role==='staff'?'Reminder received':'Reminder sent'} · ${stamp(q.active_reminders.at(-1).at_utc)}</p>`:''}
    ${q.source_finding?`<details><summary>Original passages behind this question</summary>${q.source_finding.citations.map(c=>`<a href="#reader-${esc(c.document_id)}">${esc(c.title)} - page ${c.document_page}</a><blockquote class="quote">${esc(c.quote)}</blockquote>`).join('')}<p class="muted">Source text captured when the question was created.</p></details>`:''}
    <div class="conversation">${conversationHistory(q)}</div>
    ${role==='staff'?staffEditor(q):patientThreadActions(q)}</article>`;
}
function conversationPreview(q) {
  const latest=threadMessages(q).at(-1),hasPreview=latest&&(latest.kind==='reply'||q.messages.length>1);
  const draft=role==='staff'&&waiting(q)&&state.replies.some(r=>!r.delivered&&r.covered.includes(q.id));
  return `<a class="card request-link conversation-preview" href="#thread-${q.id}"><div class="row"><h2 class="question-text">${esc(q.text)}</h2>${chip(q)}</div>${hasPreview?`<p class="preview-text"><small>${latest.kind==='reply'?'Latest ward reply':'Latest patient follow-up'}</small>${esc(latest.text)}</p>`:''}${draft?'<small>Saved draft available</small>':''}<span>${role==='staff'?waiting(q)?'Open and reply':'Open conversation':q.status==='answered'?'Read the reply and respond':'Open conversation'} &rarr;</span></a>`;
}
const batchBar=()=>'<section class="batchbar" aria-label="Send replies together"><strong>Send replies together</strong><p>Only included replies will be sent. You can edit them before sending.</p><div class="actions"><button data-action="select-replies">Select written replies</button><button class="primary" id="send-selected" data-action="send-batch" disabled>Send selected (0)</button></div></section>';
function batchView() {
  const questions=staffQuestions().filter(waiting);
  return `${breadcrumbs('Reply to several questions')}<h1>Reply to several questions</h1><p>Write and select the replies you want to send together. Leave other questions for later.</p>${batchBar()}<div class="stack">${questions.map(q=>`<section class="card"><h2 class="question-text"><a href="#thread-${q.id}">${esc(q.text)}</a></h2>${q.messages.length>1?`<p class="preview-text"><small>Latest follow-up</small>${esc(q.messages.at(-1).text)}</p>`:''}${staffEditor(q,true)}</section>`).join('')||empty('No questions are waiting for a reply.')}</div>`;
}
function requestsView(resolved=false) {
  if(role==='staff'){
    const questions=staffQuestions(resolved);
    return `<h1>${resolved?'Resolved questions':'Open questions'}</h1><p>${resolved?'These conversations were marked as resolved by the patient. Open one to read its history.':'Open a conversation to read and reply. It stays open until the patient confirms it is resolved.'}</p>
      <nav class="conversation-filters" aria-label="Filter questions">${navLink('requests',`Open (${staffQuestions().length})`,!resolved)}${navLink('resolved',`Resolved (${staffQuestions(true).length})`,resolved)}</nav>
      <div class="actions">${!resolved&&questions.some(waiting)?'<a class="button" href="#reply-batch">Reply to several questions</a>':''}${config.ai_enabled?'<a class="button" href="#ai">AI help: documents and explanations</a>':''}</div><div class="stack">${questions.length?questions.map(conversationPreview).join(''):empty(resolved?'No resolved conversations yet.':'No open questions. You can find previous conversations under Resolved.')}</div>`;
  }
  return `<h1>My questions</h1><p>Open a question to read the reply and continue the same conversation.</p><div class="actions"><a class="button primary" href="#questions">New question</a>${personal()?'<button data-action="export">Export questions and conversations</button>':''}</div>${state.closed?'<p class="ack">You confirmed that all questions are resolved.</p>':''}
    <div class="stack">${state.questions.length?state.questions.map(conversationPreview).join(''):empty('You have not asked any questions yet.')}</div>`;
}
function askView() {
  const questions=Array.isArray(drafts.question_fields)&&drafts.question_fields.length?drafts.question_fields:[drafts.questions||''];
  return `${breadcrumbs('New question')}<h1>What would you like to clarify?</h1><p>Use one box for each question. You can write several paragraphs in the same box. Add another question only when you want a separate conversation.</p>
    <form id="ask-form" class="card"><div class="stack">${questions.map((text,index)=>`<div class="question-field"><div class="question-field-heading"><label for="question-${index}">Question ${index+1}</label>${questions.length>1?`<button type="button" data-action="remove-question" data-index="${index}" aria-label="Remove question ${index+1}">Remove</button>`:''}</div><textarea id="question-${index}" name="question" rows="4" maxlength="2000" aria-describedby="question-help" placeholder="What would you like the ward to explain?">${esc(text)}</textarea></div>`).join('')}</div>
    <p id="question-help" class="muted">Up to 10 questions, 2,000 characters each. Empty boxes are not sent.</p><button type="button" data-action="add-question" ${questions.length>=10?'disabled':''}>Add another question</button>
    ${config.ai_enabled?`<p>First, look for answers in the documents available to you. Nothing is sent to the ward until you choose which questions still need a reply.</p><details><summary>Choose documents to check</summary><p class="muted">Include the documents that may answer your question.</p>${state.documents.map(d=>`<label class="check"><input type="checkbox" name="answer-document" value="${esc(d.identifier)}" checked data-persist="answer-doc-${d.identifier}">${esc(d.title)}</label>`).join('')||'<p>No documents available yet.</p>'}</details>`:''}
    <div class="actions">${config.ai_enabled?`<button class="primary" value="check" ${state.ai_pending||!state.documents.length?'disabled':''}>Check my documents first</button><button value="send">Send to ward without a check</button>`:`<button class="primary" value="send">${personal()?'Prepare questions':'Send questions to the ward'}</button>`}<a class="button" href="#requests">Back to questions</a></div>${config.ai_enabled&&(state.ai_pending||!state.documents.length)?`<p class="muted">${state.ai_pending?'Another AI check is running. You can wait or send your questions directly.':'Add a document to use the AI check, or send your questions directly.'}</p>`:''}</form>`;
}
function questionCheckView(id) {
  const job=state.ai_jobs.find(j=>j.id===id&&j.kind==='answer');
  if(!job)return breadcrumbs('Question check','ai','AI help')+empty('Question check unavailable.');
  const labels={found:'Relevant answer found in your documents',partial:'Only part of the answer was found',not_found:'No answer found in the checked documents',conflicting:'The documents contain conflicting instructions'};
  const ready=job.status==='completed'&&!job.stale;
  return `${breadcrumbs('Before contacting the ward','ai','AI help')}<h1>Is the answer already in your documents?</h1><p>No question is sent automatically. Read the original passages, then decide which questions still need the ward.</p><p class="muted">Checked documents: ${job.payload.documents.map(id=>esc(state.documents.find(d=>d.identifier===id)?.title||id)).join(', ')}. Other documents are outside this check.</p>${jobStatus(job)}
    ${ready?`<div class="stack">${job.result.answers.map(r=>{const sent=(job.asked_keys||[]).includes(r.question_id);return `<section class="card"><h2 class="question-text">${esc(r.question)}</h2><p><strong>${labels[r.status]}</strong></p>${r.citations.map(c=>`<a href="#reader-${esc(c.document_id)}">${esc(c.title)} - page ${c.document_page}</a><blockquote class="quote">${esc(c.quote)}</blockquote>`).join('')}${r.status==='found'?'<p>These are source excerpts selected by AI, not a new clinical instruction. If they answer your question, leave it unselected. You can still ask the ward.</p>':r.status==='conflicting'?'<p>Ask the ward to clarify; do not choose between the conflicting instructions here.</p>':'<p>The documents may not contain everything needed. You can ask the ward or add another document.</p>'}<label class="check"><input type="checkbox" name="checked-question" value="${r.question_id}" data-persist="checked-${job.id}-${r.question_id}" ${sent?'disabled':r.status==='found'?'':'checked'}>${sent?'Already sent to the ward':'Ask the ward about this question'}</label></section>`;}).join('')}</div><div class="actions"><button class="primary" data-action="send-checked" data-job="${job.id}">Send selected questions</button><a class="button" href="#documents">I have what I need</a></div>`:`<section class="card"><h2>Your questions</h2>${job.payload.questions.map(q=>`<p class="question-text">${esc(q.text)}</p>`).join('')}</section>`}
    <div class="actions"><button data-action="edit-checked" data-job="${job.id}">${ready?'Edit or check remaining questions':'Edit questions or send directly'}</button><a class="button" href="#add">Add another document</a></div>`;
}
function settingsView() {
  return `<h1>Your session</h1><div class="card"><p>${personal()?'PDFs, text and conversations stay on this computer, outside the project and published pages. No ward is connected and documents stay on this computer.':'This demo lets you switch between patient and ward views using example identities.'}</p><p>You can resume this session for 12 hours in the same browser. After expiry, this session is no longer available. You can delete it now, including its documents.</p><div class="actions"><button data-action="delete">Delete this session and its documents</button><a class="button" href="#${role==='staff'?'requests':'documents'}">Back to the case</a></div></div>`;
}
function shareView() {
  if(personal())return empty('Personal workspaces cannot be shared.');
  return `${breadcrumbs('Connect a device','documents','Documents')}<h1>One case, on two devices.</h1><p>Create a link and share it with the other person. Open that link on the other device without starting a new demo. Both screens should display the same case code.</p><div class="card"><h2>Case ${esc(state.workspace_code)}</h2><div class="actions"><button class="primary" data-action="invite" data-role="patient">Create patient link</button><button data-action="invite" data-role="staff">Create ward link</button></div><p class="muted">The link lasts 10 minutes, works once and replaces the previous link. Access expires with the original session. You can still switch roles in this demo.</p>${inviteLink?`<label for="invite-link">Link to share</label><input id="invite-link" class="invite-link" readonly value="${esc(inviteLink)}"><button data-action="copy-invite">Copy link</button>`:''}</div><p>Questions, replies and documents update automatically. Ward PDFs remain private until shared with the patient.</p>`;
}
function render() {
  document.body.classList.toggle('staff',role==='staff');updateNotifications();if(!role){app.innerHTML=landing();return;}
  if(!state){app.innerHTML=`<main class="landing"><h1>Open your session</h1><p>Your session is missing or has expired.</p><a class="button primary" href="${base}/">Back to start</a></main>`;return;}
  const current=page();let content;
  if(current.startsWith('thread-')){const q=state.questions.find(q=>q.id===current.slice(7)),parent=role==='staff'&&q?.status==='resolved'?'resolved':'requests',label=role==='staff'?(parent==='resolved'?'Resolved questions':'Open questions'):'My questions';content=breadcrumbs('Conversation',parent,label)+(q?`<h1>Conversation</h1>${threadCard(q)}<div class="actions"><a class="button" href="#${parent}">Back to ${label.toLowerCase()}</a>${personal()?'<button data-action="export">Export conversations</button>':''}</div>`:empty('Question unavailable.'));}
  else if(['requests','status','answers','request','review'].includes(current))content=requestsView();
  else if(current==='resolved'&&role==='staff')content=requestsView(true);
  else if(current==='activity'&&role==='staff')content=activityView();
  else if(current==='reply-batch'&&role==='staff')content=batchView();
  else if(current==='ai')content=aiView();
  else if(current==='explain'||current.startsWith('explain-'))content=explainView(current.slice(8));
  else if(current.startsWith('question-check-')&&role==='patient')content=questionCheckView(current.slice(15));
  else if(current==='check')content=checkView();else if(current==='questions')content=askView();else if(current==='add'||current.startsWith('add-for-'))content=uploadForm(current.startsWith('add-for-')?current.slice(8):null);
  else if(current.startsWith('upload-'))content=reviewUpload(current.slice(7));else if(current.startsWith('reader-'))content=readerView(current.slice(7));
  else if(current==='share')content=shareView();else if(current==='settings')content=settingsView();else content=documentsView();
  app.innerHTML=shell(content);
  app.querySelectorAll('.glossary-term,.custom-explanation').forEach(el=>{el.dataset.persist=`details-${current}-${el.querySelector('summary').textContent}`;});
  restore();updateRetryButtons();
}
const selected=name=>[...app.querySelectorAll(`input[name="${name}"]:checked:not(:disabled)`)].map(el=>el.value);
function updateSelection() {
  const send=document.querySelector('#send-selected');
  if(send){const count=selected('send-question').length;send.textContent=`Send selected (${count})`;send.disabled=!count;}
  const checked=app.querySelector('[data-action="send-checked"]');
  if(checked){const count=selected('checked-question').length;checked.textContent=`Send selected questions (${count})`;checked.disabled=!count;}
}
async function remind(ids) {
  if(!ids.length)return notice('Select at least one question.');
  if(await confirmAction(personal()?'Prepare a reminder':'Send reminders for selected questions',`<p>${personal()?'Text will be prepared for export. The app will not send it.':'The ward will see a reminder only for the questions listed here.'}</p><ul>${ids.map(id=>`<li>${esc(state.questions.find(q=>q.id===id).text)}</li>`).join('')}</ul>`,personal()?'Prepare':'Send reminder'))
    await mutate('remind',{questions:ids,confirmed:true},personal()?'Reminder recorded: export and send through your usual channel.':'Reminder visible to the ward.',undefined,ids.map(id=>'remind-'+id));
}
function exportConversations() {
  const lines=['AfterWard - Questions prepared by the patient','Send through your usual contact channel for the ward.',''];
  for(const q of state.questions){lines.push(q.text,`Local status: ${statusLabel(q)}`);for(const m of threadMessages(q))lines.push(`${m.kind==='patient'?'Patient':`Reply recorded (${m.author})`}: ${m.text}`);if(q.active_reminders.length)lines.push('Reminder: please provide an update on this question.');lines.push('');}
  const objectUrl=URL.createObjectURL(new Blob([lines.join('\n')],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=objectUrl;a.download='afterward-questions.txt';a.click();setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
}
async function readPage(uploadId,number) {
  remember();
  const previous=state.uploads.find(u=>u.id===uploadId).pages.find(p=>p.number===number).text;
  activeRead=`${uploadId}-${number}`;render();
  try{
    state=await request(`${role}/read_page`,{revision:state.revision,upload_id:uploadId,page:number});
    const key=`upload-${uploadId}-${number}`;
    if(drafts[key]===previous)delete drafts[key];
  }finally{activeRead=null;render();}
}
async function readUpload(uploadId) {
  activeUpload=uploadId;render();
  try{
    for(const p of state.uploads.find(u=>u.id===uploadId).pages){if(!p.gemma)await readPage(uploadId,p.number);}
  }finally{activeUpload=null;render();}
}
app.addEventListener('input',remember);app.addEventListener('change',event=>{
  remember();updateSelection();
});
app.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action],[data-start]');if(!el||busy)return;
  try{
    if(el.dataset.start){await request('start',{route:el.dataset.start,acknowledged:true});location.href=url(el.dataset.start==='before_delivery'?'staff':'patient',el.dataset.start==='patient'?'add':'documents');return;}
    const action=el.dataset.action;
    if(action==='add-question'||action==='remove-question'){
      remember();const fields=drafts.question_fields||[''];let focusIndex=fields.length;
      if(action==='add-question'){if(fields.length>=10)return;fields.push('');}
      else {const index=Number(el.dataset.index);if(!Number.isInteger(index)||index<0||index>=fields.length||fields.length===1)return;fields.splice(index,1);focusIndex=Math.min(index,fields.length-1);}
      drafts.question_fields=fields;render();remember();document.querySelector(`#question-${focusIndex}`)?.focus();return;
    }
    if(action==='toggle-menu'){
      mobileMenuOpen=!mobileMenuOpen;el.setAttribute('aria-expanded',String(mobileMenuOpen));el.textContent=mobileMenuOpen?'Close':'Options';
      document.querySelector('#account-tools').classList.toggle('is-open',mobileMenuOpen);return;
    }
    if(action==='join'){busy=true;const result=await request('join',{code:joinCode()});location.replace(url(result.role,result.role==='patient'?'documents':'requests'));return;}
    if(action==='invite'){const result=await request('invite',{role:el.dataset.role});inviteLink=`${config.share_base||location.origin+base}/#join=${result.code}`;render();notice('Link ready: open it on the other device within 10 minutes.');}
    if(action==='copy-invite'){try{await navigator.clipboard.writeText(inviteLink);notice('Link copied.');}catch(error){const field=document.querySelector('#invite-link');field.focus();field.select();notice('Selected: copy the link.');}}
    if(action==='refresh'){remember();state=await request(role);render();notice('Case refreshed.');}
    if(action==='send-checked')await mutate('ask_checked',{job_id:el.dataset.job,keys:selected('checked-question')},'Selected questions are available to the ward.','requests');
    if(action==='edit-checked'){
      const job=state.ai_jobs.find(j=>j.id===el.dataset.job);if(!job)return;
      drafts.question_fields=job.payload.questions.filter(q=>!(job.asked_keys||[]).includes(q.id)).map(q=>q.text);
      if(!drafts.question_fields.length)drafts.question_fields=[''];
      if(!personal())sessionStorage.setItem(storageKey(),JSON.stringify(drafts));location.hash='questions';
    }
    if(action==='ask-findings')await mutate('ask_findings',{job_id:el.dataset.job,keys:selected('finding')},'Selected questions are available to the ward.','requests');
    if(action==='retry-glossary')await mutate('retry_glossary',{document_id:el.dataset.document},'Preparing medical explanations.');
    if(action==='read-upload'){
      busy=true;
      try{await readUpload(el.dataset.upload);}finally{busy=false;}
    }
    if(action==='ask-explanation'||action==='ask-glossary'){
      const explanation=action==='ask-glossary'?state.glossaries?.[el.dataset.document]?.result?.terms[Number(el.dataset.index)]:state.explanations.find(e=>e.id===el.dataset.id);if(!explanation)return;
      remember();const fields=(drafts.question_fields||[]).filter(text=>text.trim());
      if(fields.length>=10)return notice('Send or remove a draft question before adding another.');
      const quote=explanation.source_quote.slice(0,1800);
      fields.push(`Could you explain what this passage means for me?\n\n${quote}${explanation.source_quote.length>1800?'...':''}`);
      drafts.question_fields=fields;if(!personal())sessionStorage.setItem(storageKey(),JSON.stringify(drafts));
      location.hash='questions';notice('Question prepared. Review it before sending; nothing has been sent yet.');return;
    }
    if(action==='approve-explanation')await mutate('approve_explanation',{explanation_id:el.dataset.id},'Explanation marked as reviewed by the ward.');
    if(action==='draft-ai'){
      const documents=selected('draft-document-'+el.dataset.question);
      if(!documents.length)return notice('Choose at least one document under Documents for the reply suggestion.');
      const pages=documents.reduce((sum,id)=>{const doc=state.documents.find(d=>d.identifier===id);return sum+(state.uploads.find(u=>u.id===doc.upload_id)?.pages.length||1);},0);
      if(pages>5)return notice('Choose documents totalling no more than five pages under Documents for the reply suggestion.');
      await mutate('start_ai',{kind:'draft',question_id:el.dataset.question,documents},'Preparing an optional draft. You can keep writing your own reply.');
    }
    if(action==='use-ai-draft'){
      const job=state.ai_jobs.find(j=>j.id===el.dataset.job);
      if(!job||job.stale)return notice('Request a new draft for the current conversation.');
      const editor=document.querySelector(`#reply-${el.dataset.question}`);
      if(editor.value.trim()&&!await confirmAction('Replace the text in this editor?',`<p>Your current unsaved text will be replaced with the AI suggestion. The reply will remain a draft.</p>`,'Use suggestion'))return;
      editor.value=job.result.text;remember();editor.focus();notice('Review and edit the suggestion before sending your reply.');
    }
    if(action==='read-page'){
      busy=true;el.disabled=true;el.textContent='AfterWard is reading this page...';
      try{await readPage(el.dataset.upload,Number(el.dataset.page));notice('The text is ready to compare with the page.');}
      finally{busy=false;if(el.isConnected){el.disabled=false;el.textContent=`Read page ${el.dataset.page} again`;}}
    }
    if(action==='select-replies'){const inputs=[...app.querySelectorAll('input[name="send-question"]')].filter(i=>document.querySelector('#reply-'+i.value).value.trim());const checked=!inputs.every(i=>i.checked);inputs.forEach(i=>i.checked=checked);remember();updateSelection();}
    if(action==='send-batch'){
      const ids=selected('send-question'),rows=ids.map(id=>{const field=document.querySelector('#reply-'+id);return{question_id:id,text:field.value.trim(),reply_id:field.form.dataset.reply||undefined};});
      if(!rows.length||rows.some(r=>!r.text))return notice('Write a reply for every included question.');
      if(await confirmAction(`Send ${ids.length} ${ids.length===1?'reply':'replies'} to ${state.person}`,`<p>Only these replies will be sent.</p>${rows.map(r=>`<section class="quote"><strong>${esc(state.questions.find(q=>q.id===r.question_id).text)}</strong><p>${esc(r.text)}</p></section>`).join('')}`,'Send replies'))
        await mutate('reply_and_send',{replies:rows,recipient_confirmed:true},'Replies available to the patient.','requests',ids.flatMap(id=>['batch-'+id,'reply-'+id]));
    }
    if(action==='understood')await mutate('acknowledge',{question_id:el.dataset.question,reply_id:el.dataset.reply},'Question resolved. You can reopen it in this conversation.');
    if(action==='remind-one')await remind([el.dataset.question]);if(action==='export')exportConversations();
    if(action==='share-documents'){
      remember();const ids=selected(el.dataset.selection||'document');if(!ids.length)return notice('Select at least one document.');
      if(await confirmAction('Share selected documents',`<p>Recipient: ${esc(state.person)}.</p><ul>${ids.map(id=>`<li>${esc(state.documents.find(d=>d.identifier===id).title)}</li>`).join('')}</ul>`,'Share documents')){
        busy=true;state=await request(role+'/confirm_bundle',{revision:state.revision,documents:ids,reviewed:true});state=await request(role+'/deliver_documents',{revision:state.revision});render();notice('Documents available to the patient.');busy=false;
      }
    }
    if(action==='delete'&&await confirmAction('Delete this session','<p>This session, its documents, extracted text and conversations will be removed from the database.</p>','Delete')){await request('delete',{});if(!personal())sessionStorage.removeItem(storageKey());drafts={};state=null;location.href=base+'/';}
  }catch(error){busy=false;notice(error.message);}
});
app.addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;const form=event.target;
  try{
    if(form.id==='check-form')await mutate('start_ai',{kind:'analyze',documents:selected('check-document')},'Document check started. Progress appears here.');
    else if(form.classList.contains('explain-form'))await mutate('start_ai',{kind:'explain',documents:[form.dataset.document],quote:form.elements.quote.value},'Explaining the passage. The result will appear here.');
    else if(form.id==='ask-form'){
      const questions=[...form.querySelectorAll('textarea[name="question"]')].map(el=>el.value.trim()).filter(Boolean);
      if(!questions.length||questions.length>10||questions.some(q=>q.length>2000))return notice('Enter 1 to 10 questions, up to 2,000 characters each.');
      if(event.submitter?.value==='check'){
        const documents=selected('answer-document');if(!documents.length)return notice('Choose at least one document to check, or send your questions directly.');
        if(await mutate('start_ai',{kind:'answer',documents,questions},'Checking your documents. No questions have been sent.',undefined,['questions','question_fields']))location.hash='question-check-'+state.ai_jobs.filter(j=>j.kind==='answer').at(-1).id;
      }else await mutate('ask',{questions},personal()?'Questions prepared. You can now export them.':'Questions available to the ward.','requests',['questions','question_fields']);
    }
    else if(form.classList.contains('reply-form')){
      const q=form.dataset.question;
      if(event.submitter?.value==='send')await mutate('reply_and_send',{replies:[{question_id:q,text:form.elements.text.value,reply_id:form.dataset.reply||undefined}],recipient_confirmed:true},'Reply available to the patient.',undefined,['reply-'+q,'batch-'+q]);
      else await mutate('draft',{text:form.elements.text.value,covered:[q],reply_id:form.dataset.reply||undefined},'Draft saved for later.',undefined,['reply-'+q]);
    }
    else if(form.classList.contains('follow-form'))await mutate('follow_up',{question_id:form.dataset.question,reply_id:form.dataset.reply,text:form.elements.text.value},'Follow-up added to the same conversation.',undefined,['follow-'+form.dataset.question]);
    else if(form.classList.contains('external-form'))await mutate('external_reply',{question_id:form.dataset.question,text:form.elements.text.value,source:form.elements.source.value},'Reply recorded with the source you provided.',undefined,['external-'+form.dataset.question]);
    else if(form.id==='upload-form'){
      const file=form.elements.pdf.files[0];if(!file||file.size>config.pdf_max_bytes)return notice('Choose a PDF up to 12 MiB.');busy=true;form.querySelector('button').disabled=true;document.querySelector('#upload-progress').innerHTML=loading('opening your PDF');
      const response=await fetch(`${base}/api/${role}/upload`,{method:'POST',headers:{'Content-Type':'application/pdf','X-AfterWard-Request':'1','X-AfterWard-CSRF':state.csrf,'X-AfterWard-Revision':String(state.revision),'X-AfterWard-Filename':encodeURIComponent(file.name)},body:file});const value=await response.json();if(!response.ok)throw new Error(errorMessage(value));state=value;const upload=state.uploads.at(-1);if(form.dataset.returnThread)drafts['upload-thread-'+upload.id]=form.dataset.returnThread;activeUpload=upload.id;location.hash='upload-'+upload.id;render();
      if(config.ocr_provider==='amd_api')await readUpload(upload.id);else{activeUpload=null;render();}
    }else if(form.id==='confirm-upload'){
      const questionId=uploadThread(form.dataset.upload);
      if(questionId)drafts['reply-documents-open-'+questionId]=true;
      await mutate('confirm_upload',{upload_id:form.dataset.upload,pages:[...form.querySelectorAll('[name="page"]')].map(el=>el.value),reviewed:form.elements.reviewed.checked},questionId?'Document added to the case. Choose whether to share it with the patient.':'Document added with reviewed text.',questionId?'thread-'+questionId:'reader-pdf-'+form.dataset.upload,state.uploads.find(u=>u.id===form.dataset.upload).pages.map(p=>`upload-${form.dataset.upload}-${p.number}`));
    }
  }catch(error){notice(error.message);const button=form.querySelector('button');if(button)button.disabled=false;}finally{busy=false;}
});
window.addEventListener('hashchange',()=>{if(page()==='main'){document.querySelector('#main')?.focus();return;}mobileMenuOpen=false;remember();render();document.querySelector('#main')?.scrollIntoView({block:'start'});});
window.addEventListener('beforeunload',remember);
document.querySelector('#dismiss-notice').addEventListener('click',dismissNotice);
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!document.querySelector('#confirm').open)dismissNotice();});
document.querySelector('.skip').addEventListener('click',event=>{
  event.preventDefault();const main=document.querySelector('#main');if(main){main.tabIndex=-1;main.focus();main.scrollIntoView({block:'start'});}
});
async function start(){try{config=await request('config');if(role){state=await request(role);if(!personal())drafts=JSON.parse(sessionStorage.getItem(storageKey())||'{}');}
  else if(personal()){try{state=await request('patient');}catch(error){state=null;}}
  render();}catch(error){if(config)render();else app.innerHTML='<main class="landing"><h1>AfterWard is unavailable</h1><p>Reload after starting the service.</p></main>';notice(error.message);}}
start();
let polling=false;
async function refreshWhenIdle(){
  if(!role||!state||busy||polling||document.hidden||document.querySelector('#confirm').open)return;
  if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;
  polling=true;
  try{const latest=await request(role);if(latest.csrf===state.csrf&&(latest.revision>state.revision||latest.ai_revision>state.ai_revision||latest.glossary_revision!==state.glossary_revision)&&!busy
      &&!document.querySelector('#confirm').open&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){
      const conversationChanged=latest.revision>state.revision;remember();state=latest;render();if(conversationChanged)notice('The conversation was updated.');}}
  catch(error){const control=app.querySelector('[data-action="refresh"]');if(control)control.textContent='Check connection: refresh';}
  finally{polling=false;}
}
function updateRetryButtons() {
  app.querySelectorAll('[data-retry-at]').forEach(button=>{
    const seconds=Math.max(0,Math.ceil((Date.parse(button.dataset.retryAt)-Date.now())/1000));
    button.disabled=seconds>0||Boolean(state?.ai_pending);
    button.textContent=seconds>0?`Try again in ${seconds} s`:'Try reply suggestion again';
  });
}
setInterval(updateRetryButtons,1000);
setInterval(refreshWhenIdle,3000);
window.addEventListener('focus',refreshWhenIdle);
document.addEventListener('visibilitychange',refreshWhenIdle);
