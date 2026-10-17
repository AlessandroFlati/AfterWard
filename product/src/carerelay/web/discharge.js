'use strict';
const app = document.querySelector('#app');
const path = location.pathname.replace(/\/$/, '');
const role = path.endsWith('/staff') ? 'staff' : path.endsWith('/patient') ? 'patient' : null;
const base = role ? path.replace(/\/(staff|patient)$/, '') : path;
let state = null, config = null, busy = false, drafts = {}, inviteLink = '', activeRead = null;
const joinCode = () => location.hash.startsWith('#join=') ? location.hash.slice(6) : null;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const url = (area, page='') => `${base}/${area}${page ? '#' + page : ''}`;
const stamp = value => value ? new Date(value).toLocaleString('en-GB', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : '';
const personal = () => config.personal;
const page = () => location.hash.slice(1) || (role === 'staff' ? 'requests' : 'documents');
const waiting = q => ['open','needs_review'].includes(q.status);
const staffQuestions = () => [...state.questions].sort((a,b)=>{
  const priority=q=>q.active_reminders.length?0:waiting(q)?1:q.status==='answered'?2:3;
  return priority(a)-priority(b);
});
const statusLabel = q => ({open:personal()?'Ready to contact':'Waiting for the ward',needs_review:'Needs review',answered:personal()?'Reply recorded':role==='staff'?'Waiting for the patient':'Reply available',resolved:role==='staff'?'Resolved by the patient':'Resolved by you'}[q.status] || q.status);
const storageKey = () => `afterward-thread-forms-${state.csrf}-${role}`;
function notice(text) {document.querySelector('#notice').textContent=text;clearTimeout(notice.timer);notice.timer=setTimeout(()=>document.querySelector('#notice').textContent='',10000);}
function remember() {
  app.querySelectorAll('[data-persist]').forEach(el=>drafts[el.dataset.persist]=el.type==='checkbox'?el.checked:el.value);
  if(state&&!personal())sessionStorage.setItem(storageKey(),JSON.stringify(drafts));
}
function restore() {
  app.querySelectorAll('[data-persist]').forEach(el=>{if(Object.hasOwn(drafts,el.dataset.persist)){if(el.type==='checkbox')el.checked=drafts[el.dataset.persist];else el.value=drafts[el.dataset.persist];}});
  updateSelection();
}
function confirmAction(title,body,submit='Confirm') {
  const dialog=document.querySelector('#confirm');document.querySelector('#confirm-title').textContent=title;
  document.querySelector('#confirm-body').innerHTML=body;document.querySelector('#confirm-submit').textContent=submit;
  dialog.returnValue='';dialog.showModal();return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
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
    render();if(target&&page()!==target)location.hash=target;notice(success);
  }catch(error){notice(error.message);}finally{busy=false;app.removeAttribute('aria-busy');}
}
const chip=q=>`<span class="chip ${q.status==='resolved'?'good':waiting(q)?'open':'progress'}">${esc(statusLabel(q))}</span>`;
const empty=text=>`<div class="empty">${text}</div>`;
function breadcrumbs(title,parent='requests',label=role==='staff'?'All questions':'My questions') {
  return `<nav class="breadcrumbs" aria-label="Breadcrumbs"><a href="#${parent}">${label}</a><span aria-hidden="true">/</span><span>${esc(title)}</span></nav>`;
}
function landing() {
  if(joinCode()&&!personal())return `<main class="landing"><a class="brand" href="${base}/">After<span>Ward</span></a><h1>Open the shared case</h1><p>Join the case of the person who sent this link. Questions, replies and PDFs will be shared across your devices.</p><p>This link expires in 10 minutes and works once. It will replace the current demo session in this browser.</p><button class="primary" data-action="join">Open the case</button></main>`;
  if(personal())return `<main class="landing" id="main"><a class="brand" href="${base}/">After<span>Ward</span></a><div class="eyebrow">Personal workspace on your computer</div>
    <h1>Start with your discharge letter.</h1><p class="lede">Upload a PDF, compare the transcription with each page and prepare questions for a ward that has not adopted AfterWard.</p>
    <div class="card"><h2>What you can try</h2><p>Local PDF reading and OCR, linked conversations, question export and recording replies received outside the app.</p>
    <p>Documents stay on this computer. Previously saved transcriptions remain available. The shared AMD API excludes personal health data. This workspace supports local extraction and manual review. Automated gap detection is not connected here, and the app does not send questions to an external ward.</p>
    <p class="muted">Up to 5 pages and 12 MiB per PDF. Sessions last 12 hours; you can delete them in Session settings. The app does not encrypt the local database.</p>
    ${state?`<a class="button primary" href="${url('patient','documents')}">Resume your personal session</a><p class="muted">To start another case, delete this session in Session settings first.</p>`:'<button class="primary" data-start="patient">Open personal workspace</button>'}</div></main>`;
  return `<main class="landing" id="main"><a class="brand" href="${base}/">After<span>Ward</span></a><div class="eyebrow">Interactive demo</div>
    <h1>One discharge.<br>A conversation that continues.</h1><p class="lede">Patient and ward share one case. Every question keeps its own conversation.</p>
    <div class="actions"><a class="button primary" href="${base}/judge-guide">Try the guided cases</a><a class="button" href="${base}/presentation">Video and presentation walkthrough</a></div>
    <p class="notice-inline">Explore with example documents and identities. Replies are written by the person using the staff view; no external messages are sent. ${['127.0.0.1','localhost'].includes(location.hostname) ? 'For a personal PDF, use the <a href="http://127.0.0.1:8777/">separate local workspace</a>.' : 'Only invented, non-sensitive documents may be used in this demo.'}</p>
    <div class="options"><section class="card"><h2>The patient has their documents</h2><p>The letter is available. Ask a question and answer it from the ward view.</p><button class="primary" data-start="hospital">Start as a patient</button></section>
    <section class="card"><h2>The ward prepares discharge</h2><p>Review the case and choose the documents to share.</p><button data-start="before_delivery">Start as ward staff</button></section>
    <section class="card"><h2>The patient brings a PDF</h2><p>Try PDF intake, OCR and questions with an example document.</p><button data-start="patient">Start with a PDF</button></section></div>
    <p><a href="${url('patient')}">Resume patient view</a> · <a href="${url('staff')}">Resume ward view</a></p></main>`;
}
const navLink=(hash,text,active)=>`<a href="#${hash}" ${active?'aria-current="page"':''}>${text}</a>`;
function shell(content) {
  const current=page(), requests=['requests','status','answers','questions'].includes(current)||current.startsWith('thread-');
  const reminders=state.questions.filter(q=>q.active_reminders.length);
  const top=`<header class="appbar"><a class="brand" href="${base}/">After<span>Ward</span></a><nav aria-label="Main navigation">
    ${navLink(role==='staff'?'requests':'documents',role==='staff'?'Questions':'Documents',role==='staff'?requests:!requests&&current!=='settings')}
    ${role==='staff'?navLink('documents','Documents',current==='documents'||current.startsWith('reader-')||current.startsWith('upload-')):navLink('requests','My questions',requests)}
    ${navLink('settings','Session',current==='settings')}<a href="${base}/judge-guide" target="_blank" rel="noopener">Guide</a></nav><div class="account"><span>${esc(personal()?'Personal workspace':role==='staff'?'Marta Ricci · Ward staff':state.person)}</span><button data-action="refresh">Refresh</button>
    ${!personal()?`<a class="button" href="${url(role==='staff'?'patient':'staff','requests')}">View ${role==='staff'?'patient':'ward'}</a>`:''}</div></header>${!personal()?`<div class="collaboration-bar"><span>Case ${esc(state.workspace_code||'condivisa')} · automatically updated</span><a href="#share">Connect another device</a></div>`:''}`;
  if(role!=='staff')return `${top}<main class="patient-shell" id="main">${personal()?'<p class="local-status">Documents on your computer · No AMD API for personal data · Ward not connected</p>':''}${content}</main>`;
  return `${top}<div class="staff-shell"><aside class="queue"><div class="eyebrow">Current case</div><h2>${esc(state.person)}</h2><p class="mono">${esc(state.episode_id)}</p>
    ${reminders.length?`<a class="reminder-banner" href="#requests"><strong>${reminders.length} ${reminders.length===1?'question with a reminder':'questions with reminders'}</strong><span>Open the highlighted conversations</span></a>`:''}
    <nav aria-label="Conversations"><a class="button wide" href="#requests">All questions (${state.questions.length})</a>${staffQuestions().map(q=>`<a class="card ${current==='thread-'+q.id?'selected':''}" href="#thread-${q.id}">${q.active_reminders.length?'<span class="reminder-label">REMINDER</span>':''}<strong>${esc(q.text)}</strong><br>${chip(q)}</a>`).join('')}</nav>
    <a class="button wide" href="#documents">Open documents</a></aside><main class="workspace" id="main">${content}</main></div>`;
}
function documentsView() {
  return `<h1>${role==='staff'?'Discharge documents':'Your documents'}</h1><p class="lede">${role==='staff'?'Review the case and choose what to share with the patient.':'Open a document or add a PDF. Find questions and replies under My questions.'}</p>
    <div class="actions"><a class="button primary" href="#add">Upload a PDF</a><a class="button" href="#requests">${role==='staff'?'Open questions':'Open questions and replies'}</a></div>
    ${state.uploads.filter(u=>!u.confirmed).map(u=>`<a class="card request-link open" href="#upload-${u.id}"><strong>${esc(u.title)}</strong><span>Text extracted: review each page to finish adding this document</span></a>`).join('')}
    <div class="stack">${state.documents.length?state.documents.map(d=>`<section class="card"><div class="row"><h2><a href="#reader-${d.identifier}">${esc(d.title)}</a></h2><span class="chip">${d.origin==='personal'?'Added by you':d.delivered?'Available to the patient':'In the ward case file'}</span></div><p>${d.demo_pdf||d.upload_id?'PDF document · original and transcription available':esc(d.text.slice(0,180))}</p><a class="button" href="#reader-${d.identifier}">Open document</a>
      ${role==='staff'&&d.approved?`<label class="check"><input type="checkbox" name="document" value="${d.identifier}" data-persist="doc-${d.identifier}">Include when sharing</label>`:''}</section>`).join(''):empty('No documents yet. Upload your first PDF.')}</div>
    ${role==='staff'?'<div class="batchbar"><p>Documents are shared separately from replies to questions.</p><button data-action="share-documents">Share selected documents</button></div>':''}`;
}
function readerView(id) {
  const doc=state.documents.find(d=>d.identifier===id);if(!doc)return breadcrumbs('Document','documents','Documents')+empty('Document unavailable.');
  const upload=state.uploads.find(u=>u.id===doc.upload_id);
  const pdfBase=upload?`${base}/api/${role}/uploads/${upload.id}`:doc.demo_pdf?`${base}/api/${role}/documents/${doc.identifier}`:null;
  return `${breadcrumbs(doc.title,'documents','Documents')}<h1>${esc(doc.title)}</h1><div class="actions"><a class="button" href="#documents">Back to documents</a>${pdfBase?`<a class="button" href="${pdfBase}/0" target="_blank" rel="noopener">Open original PDF</a>`:''}${role!=='staff'?'<a class="button primary" href="#questions">Ask about this document</a>':'<a class="button" href="#requests">Back to questions</a>'}</div>
    ${upload?upload.pages.map(p=>`<section class="card"><h2>Page ${p.number}</h2><div class="pdf-columns"><a href="${pdfBase}/${p.number}" target="_blank" rel="noopener"><img class="pdf-page" src="${pdfBase}/${p.number}" alt="Original, page ${p.number}"></a><div><p class="muted">${p.method==='gemma_api'?'Gemma via AMD API':p.method==='ocr'?'OCR transcription':'PDF text'} · Reviewed by the person who added it</p><div class="quote">${esc(p.text)}</div></div></div>${readingComparison(upload,p)}</section>`).join(''):doc.demo_pdf?`<article class="card"><a href="${pdfBase}/0" target="_blank" rel="noopener"><img class="pdf-page demo-original" src="${pdfBase}/1" alt="${esc(doc.title)}: original PDF page"></a><details><summary>Show document text</summary><div class="quote">${esc(doc.text)}</div></details></article>`:`<article class="card"><div class="quote">${esc(doc.text)}</div></article>`}
    <p class="muted">Automatic explanations are not connected to this screen yet.</p>`;
}
function uploadForm() {
  return `${breadcrumbs('Add PDF','documents','Documents')}<h1>Add your document</h1><p>PDFs may contain selectable text or scans. After extraction, you can review and correct every page.</p>
    <form id="upload-form" class="card"><label for="pdf-file">PDF to add (1 to 5 pages, maximum 12 MiB)</label><input id="pdf-file" name="pdf" type="file" accept="application/pdf,.pdf" required>
    <p>${config.ocr_provider==='amd_api'?'Gemma 4 31B reads each page through the AMD API. Use invented examples without personal or confidential data.':config.ocr_provider==='disabled'?'API OCR is disabled. Selectable PDF text is extracted; scans require manual transcription.':'The PDF is read on this computer. No page is sent to AMD.'}</p>
    ${config.packaged_uploads_only?`<p>This public demo accepts the <a href="${base}/judge-guide#ocr" target="_blank" rel="noopener">downloadable example PDFs</a> only. Other files are rejected before inference.</p>`:''}
    <div class="actions"><button class="primary" type="submit">Read the PDF</button><a class="button" href="#documents">Back to documents</a></div><p id="upload-progress" role="status"></p></form>`;
}
function readingComparison(upload,p) {
  const g=p.gemma;
  if(activeRead===`${upload.id}-${p.number}`)return '<section class="ocr-comparison" role="status"><h3>Gemma is reading through the AMD API</h3><p>Transcription in progress. Your PDF is saved; wait for the result before reviewing it.</p></section>';
  if(!g&&personal())return '<p class="muted">The shared AMD API does not allow personal health documents. No new local Gemma inference is started.</p>';
  if(!g&&config.ocr_provider!=='amd_api')return '';
  const differences=g&&((g.numbers_only_in_current||[]).length||(g.numbers_only_in_gemma||[]).length);
  return `<section class="ocr-comparison" aria-label="Gemma transcription, page ${p.number}"><h3>Gemma 4 31B · ${g?.provider==='local_ollama'?'saved local transcription':'AMD API'}</h3><p class="muted">Compare the transcription with the original before accepting it.</p>
    ${g?`<p class="muted">${g.elapsed_seconds} s · independent transcription</p><details><summary>Show the original Gemma transcription</summary><div class="quote transcription">${esc(g.text)}</div></details>${personal()?`<details><summary>Compare with previous OCR</summary><div class="quote transcription">${esc(p.original_text)}</div></details>`:''}`:`<p>Reading is not complete. Your PDF is saved; you can retry this page.</p><button type="button" data-action="read-page" data-upload="${upload.id}" data-page="${p.number}">Read page ${p.number} with Gemma via API</button>`}
    ${g&&p.original_text?`<p class="${differences?'notice-inline':'muted'}">${differences?`Compare numbers with the original. Only in the previous transcription: ${esc((g.numbers_only_in_current||[]).join(', ')||'none')}. Only in Gemma: ${esc((g.numbers_only_in_gemma||[]).join(', ')||'none')}.`:'Both transcriptions contain the same numbers. This check does not verify words, negations or row associations.'}</p>`:''}</section>`;
}
function reviewUpload(id) {
  const u=state.uploads.find(item=>item.id===id);if(!u)return breadcrumbs('Review PDF')+empty('Document unavailable.');if(u.confirmed)return readerView('pdf-'+id);
  return `${breadcrumbs('PDF review','documents','Documents')}<h1>Review the pages of ${esc(u.title)}</h1><p>Compare the text on the right with the original on the left. Correct numbers, names and reading order; do not fill in missing instructions.</p>
    <form id="confirm-upload" data-upload="${id}" class="stack">${u.pages.map(p=>`<section class="card"><h2>Page ${p.number} · ${p.method==='gemma_api'?'Gemma via AMD API':p.method==='gemma_pending'?'Gemma transcription pending':p.method==='ocr'?'OCR':'PDF text'}</h2>${p.warnings.map(w=>`<p class="notice-inline">${esc(w)}</p>`).join('')}
      <div class="pdf-columns"><a href="${base}/api/${role}/uploads/${id}/${p.number}" target="_blank" rel="noopener"><img class="pdf-page" src="${base}/api/${role}/uploads/${id}/${p.number}" alt="Original page ${p.number}; open to enlarge"></a><div><label for="page-${p.number}">Text to review · page ${p.number}</label><textarea id="page-${p.number}" class="page-text" name="page" data-persist="upload-${id}-${p.number}" maxlength="24000" required>${esc(p.text)}</textarea></div></div>${readingComparison(u,p)}</section>`).join('')}
    <label class="check"><input type="checkbox" name="reviewed" required>I compared every page and checked that this document belongs to this case.</label><p class="muted">This confirms the transcription and selected case. It does not verify the author or clinical correctness.</p>
    <div class="actions"><button class="primary">Accept reviewed text</button><a class="button" href="#documents">Finish later</a></div></form>`;
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
function staffEditor(q) {
  if(!waiting(q))return `<p class="muted">${q.status==='resolved'?'The patient confirmed that their question is resolved.':'Reply sent. The patient decides whether their question is resolved.'}</p>`;
  const draft=state.replies.filter(r=>!r.delivered&&r.covered.length===1&&r.covered[0]===q.id).at(-1);
  return `<form class="reply-form" data-question="${q.id}" data-reply="${draft?.id||''}"><label for="reply-${q.id}">Reply to the ${q.messages.length>1?'latest follow-up':'patient'}</label><textarea id="reply-${q.id}" name="text" maxlength="10000" data-persist="reply-${q.id}" required placeholder="Write your reply directly. No additional documents are required.">${esc(draft?.text||'')}</textarea>
    <div class="actions"><button type="submit">${draft?'Save changes':'Save draft'}</button>${draft?`<label class="check"><input type="checkbox" name="send-reply" value="${draft.id}" data-persist="send-${draft.id}">Include this reply in the selected batch</label>`:'<small>Save to select this reply and send it with others.</small>'}</div></form>`;
}
function patientThreadActions(q) {
  if(q.status==='answered'||q.status==='resolved')return `<div class="thread-outcome">${q.status==='resolved'?'<p class="ack">You confirmed this question is resolved. You can reopen the conversation if needed.</p>':`<div class="actions"><button class="primary" data-action="understood" data-question="${q.id}" data-reply="${q.answered_by}">I understand, question resolved</button></div>`}
    <details><summary>${q.status==='resolved'?'I have another question about this':'Still unclear: ask for more details'}</summary><form class="follow-form" data-question="${q.id}" data-reply="${q.answered_by}"><label for="follow-${q.id}">What would you like clarified?</label><textarea id="follow-${q.id}" name="text" maxlength="2000" data-persist="follow-${q.id}" required></textarea><button class="primary">${personal()?'Prepare follow-up':'Ask in this conversation'}</button></form></details></div>`;
  const option=state.reminder_action.options.find(o=>o.question_id===q.id);
  return `<p class="muted">${q.messages.length>1?'Your latest follow-up is above. The earlier reply stays in this conversation.':personal()?'Export this question and contact the ward through a channel you already know.':'This question is waiting for the ward.'}</p>
    <div class="actions"><button data-action="remind-one" data-question="${q.id}" ${option?.available?'':'disabled'}>${personal()?'Prepare a reminder':'Send a reminder for this question'}</button>${option&&!option.available?`<small>Next reminder available ${stamp(option.next_allowed_at_utc)}</small>`:''}</div>
    ${personal()?`<details><summary>Record a reply you received</summary><form class="external-form" data-question="${q.id}"><label>From whom and through which channel?<input name="source" maxlength="200" required placeholder="Example: outpatient clinic, phone call on..."></label><label>Reply received<textarea name="text" maxlength="10000" required data-persist="external-${q.id}"></textarea></label><button>Record in conversation</button></form></details>`:''}`;
}
function threadCard(q,full=false) {
  return `<article class="card thread-card ${q.active_reminders.length?'reminded':''}" id="question-${q.id}"><div class="row"><h2>${full?esc(q.text):`<a href="#thread-${q.id}">${esc(q.text)}</a>`}</h2>${chip(q)}</div>
    ${q.active_reminders.length?`<div class="reminder-banner"><strong>${q.active_reminders.length} reminder${q.active_reminders.length>1?'s':''} for this question</strong><span>Latest: ${stamp(q.active_reminders.at(-1).at_utc)}</span></div>`:''}
    ${!full&&q.messages.length>1?`<div class="quote"><small>Latest patient follow-up</small>${esc(q.messages.at(-1).text)}</div>`:''}
    ${full?`<div class="conversation">${conversationHistory(q)}</div>`:`<details><summary>Read the full conversation</summary><div class="conversation">${conversationHistory(q)}</div></details>`}
    ${role==='staff'?staffEditor(q):patientThreadActions(q)}</article>`;
}
const batchBar=()=>'<section class="batchbar" aria-label="Send replies"><div><strong>Send completed replies</strong><p>Select saved drafts below. The others stay in preparation.</p></div><div class="actions"><button data-action="select-replies">Select all drafts</button><button class="primary" id="send-selected" data-action="send-batch" disabled>Send selected (0)</button></div></section>';
function requestsView() {
  const notified=state.questions.filter(q=>q.active_reminders.length);
  if(role==='staff')return `<h1>Questions and replies</h1><p>Read each conversation, prepare replies and send your selection from this page.</p>${notified.length?`<div class="reminder-banner"><strong>${notified.length} ${notified.length===1?'question with a reminder':'questions with reminders'} from the patient</strong><div>${notified.map(q=>`<a href="#thread-${q.id}">${esc(q.text)}</a>`).join(' · ')}</div></div>`:''}${batchBar()}<div class="stack">${state.questions.length?staffQuestions().map(q=>threadCard(q)).join(''):empty('No questions yet. Open the patient view to start.')}</div>`;
  return `<h1>My questions</h1><p>Open a question to read the reply and continue the same conversation.</p><div class="actions"><a class="button primary" href="#questions">New question</a>${personal()?'<button data-action="export">Export questions and conversations</button>':''}</div>${state.closed?'<p class="ack">You confirmed that all questions are resolved.</p>':''}
    <div class="stack">${state.questions.length?state.questions.map(q=>{const reply=state.replies.find(r=>r.id===q.answered_by);
      return `<section class="card"><a class="request-link" href="#thread-${q.id}"><div class="row"><h2>${esc(q.text)}</h2>${chip(q)}</div><p>${esc(q.status==='answered'?reply?.text.slice(0,200):q.messages.at(-1).text)}</p><span>${q.status==='answered'?'Read the reply and respond':'Open the conversation'} →</span></a>
      </section>`;}).join(''):empty('You have not asked any questions yet.')}</div>`;
}
function askView() {
  return `${breadcrumbs('New question')}<h1>What would you like to clarify?</h1><p>Write one question per line. Each gets its own conversation, so you can follow it and send a reminder separately.</p><form id="ask-form" class="card"><label for="questions">Your questions</label><textarea id="questions" name="questions" rows="6" data-persist="questions" required placeholder="When should I start the exercises?&#10;Where is the rehabilitation sheet?"></textarea><div class="actions"><button class="primary">${personal()?'Prepare questions':'Send questions to the ward'}</button><a class="button" href="#requests">Back to questions</a></div></form>`;
}
function settingsView() {
  return `<h1>Your session</h1><div class="card"><p>${personal()?'PDFs, text and conversations stay on this computer, outside the project and published pages. No ward is connected and no data is sent to the AMD API.':'This session uses demo identities. Switching views lets you try both roles; it is not hospital authentication.'}</p><p>You can resume this session for 12 hours in the same browser. Expired data becomes inaccessible and is removed when a session is opened or the service restarts. Delete now to remove it from the database immediately.</p><div class="actions"><button data-action="delete">Delete this session and its documents</button><a class="button" href="#${role==='staff'?'requests':'documents'}">Back to the case</a></div></div>`;
}
function shareView() {
  if(personal())return empty('Personal workspaces cannot be shared.');
  return `${breadcrumbs('Connect a device','documents','Documents')}<h1>One case, on two devices.</h1><p>Create a link and share it with the other person. Open that link on the other device without starting a new demo. Both screens should display the same case code.</p><div class="card"><h2>Case ${esc(state.workspace_code)}</h2><div class="actions"><button class="primary" data-action="invite" data-role="patient">Create patient link</button><button data-action="invite" data-role="staff">Create ward link</button></div><p class="muted">The link lasts 10 minutes, works once and replaces the previous link. Access expires with the original session. You can still switch roles in this demo.</p>${inviteLink?`<label for="invite-link">Link to share</label><input id="invite-link" class="invite-link" readonly value="${esc(inviteLink)}"><button data-action="copy-invite">Copy link</button>`:''}</div><p>Questions, replies and documents update automatically. Ward PDFs remain private until shared with the patient.</p>`;
}
function render() {
  document.body.classList.toggle('staff',role==='staff');if(!role){app.innerHTML=landing();return;}
  if(!state){app.innerHTML=`<main class="landing"><h1>Open your session</h1><p>Your session is missing or has expired.</p><a class="button primary" href="${base}/">Back to start</a></main>`;return;}
  const current=page();let content;
  if(current.startsWith('thread-')){const q=state.questions.find(q=>q.id===current.slice(7));content=breadcrumbs('Conversation')+(q?`${role==='staff'?batchBar():''}${threadCard(q,true)}<div class="actions"><a class="button" href="#requests">Back to all questions</a>${personal()?'<button data-action="export">Export conversations</button>':''}</div>`:empty('Question unavailable.'));}
  else if(['requests','status','answers','request','review'].includes(current))content=requestsView();
  else if(current==='questions')content=askView();else if(current==='add')content=uploadForm();
  else if(current.startsWith('upload-'))content=reviewUpload(current.slice(7));else if(current.startsWith('reader-'))content=readerView(current.slice(7));
  else if(current==='share')content=shareView();else if(current==='settings')content=settingsView();else content=documentsView();
  app.innerHTML=shell(content);restore();
}
const selected=name=>[...app.querySelectorAll(`input[name="${name}"]:checked:not(:disabled)`)].map(el=>el.value);
function updateSelection() {
  const send=document.querySelector('#send-selected');
  if(send){const count=selected('send-reply').length;send.textContent=`Send selected (${count})`;send.disabled=!count;}
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
app.addEventListener('input',remember);app.addEventListener('change',()=>{remember();updateSelection();});
app.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action],[data-start]');if(!el||busy)return;
  try{
    if(el.dataset.start){await request('start',{route:el.dataset.start,acknowledged:true});location.href=url(el.dataset.start==='before_delivery'?'staff':'patient',el.dataset.start==='patient'?'add':'documents');return;}
    const action=el.dataset.action;
    if(action==='join'){busy=true;const result=await request('join',{code:joinCode()});location.replace(url(result.role,result.role==='patient'?'documents':'requests'));return;}
    if(action==='invite'){const result=await request('invite',{role:el.dataset.role});inviteLink=`${config.share_base||location.origin+base}/#join=${result.code}`;render();notice('Link ready: open it on the other device within 10 minutes.');}
    if(action==='copy-invite'){try{await navigator.clipboard.writeText(inviteLink);notice('Link copied.');}catch(error){const field=document.querySelector('#invite-link');field.focus();field.select();notice('Selected: copy the link.');}}
    if(action==='refresh'){remember();state=await request(role);render();notice('Case refreshed.');}
    if(action==='read-page'){
      busy=true;el.disabled=true;el.textContent='Gemma is reading through the AMD API…';
      try{await readPage(el.dataset.upload,Number(el.dataset.page));notice('Gemma transcription ready: compare it with the page.');}
      finally{busy=false;if(el.isConnected){el.disabled=false;el.textContent=`Retry page ${el.dataset.page} with Gemma via API`;}}
    }
    if(action==='select-replies'){const inputs=[...app.querySelectorAll('input[name="send-reply"]:not(:disabled)')];const checked=!inputs.every(i=>i.checked);inputs.forEach(i=>i.checked=checked);remember();updateSelection();}
    if(action==='send-batch'){
      const ids=selected('send-reply'),rows=ids.map(id=>state.replies.find(r=>r.id===id));
      if(rows.some(r=>{const editor=document.querySelector(`#reply-${r.covered[0]}`);return editor&&editor.value.trim()!==r.text;}))return notice('Save changes to selected drafts before sending them.');
      if(await confirmAction(`Send ${ids.length} replies to ${state.person}`,`<p>Selected replies will be available immediately. Other drafts stay private.</p>${rows.map(r=>`<section class="quote"><strong>${esc(state.questions.find(q=>q.id===r.covered[0]).text)}</strong><p>${esc(r.text)}</p></section>`).join('')}`,'Send replies'))
        await mutate('send_batch',{reply_ids:ids,recipient_confirmed:true},'Replies available to the patient.',undefined,[...ids.map(id=>'send-'+id),...rows.flatMap(r=>r.covered.map(id=>'reply-'+id))]);
    }
    if(action==='understood')await mutate('acknowledge',{question_id:el.dataset.question,reply_id:el.dataset.reply},'Question resolved. You can reopen it in this conversation.');
    if(action==='remind-one')await remind([el.dataset.question]);if(action==='export')exportConversations();
    if(action==='share-documents'){
      const ids=selected('document');if(!ids.length)return notice('Select at least one document.');
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
    if(form.id==='ask-form'){const questions=form.elements.questions.value.split('\n').map(s=>s.trim()).filter(Boolean);if(!questions.length||questions.length>10||questions.some(q=>q.length>2000))return notice('Enter 1 to 10 questions, up to 2,000 characters each.');await mutate('ask',{questions},personal()?'Questions prepared. You can now export them.':'Questions available to the ward.','requests',['questions']);}
    else if(form.classList.contains('reply-form')){const q=form.dataset.question;await mutate('draft',{text:form.elements.text.value,covered:[q],reply_id:form.dataset.reply||undefined},'Draft saved. You can select it to send.',undefined,['reply-'+q]);}
    else if(form.classList.contains('follow-form'))await mutate('follow_up',{question_id:form.dataset.question,reply_id:form.dataset.reply,text:form.elements.text.value},'Follow-up added to the same conversation.',undefined,['follow-'+form.dataset.question]);
    else if(form.classList.contains('external-form'))await mutate('external_reply',{question_id:form.dataset.question,text:form.elements.text.value,source:form.elements.source.value},'Reply recorded with the source you provided.',undefined,['external-'+form.dataset.question]);
    else if(form.id==='upload-form'){
      const file=form.elements.pdf.files[0];if(!file||file.size>config.pdf_max_bytes)return notice('Choose a PDF up to 12 MiB.');busy=true;form.querySelector('button').disabled=true;document.querySelector('#upload-progress').textContent='Uploading and preparing pages...';
      const response=await fetch(`${base}/api/${role}/upload`,{method:'POST',headers:{'Content-Type':'application/pdf','X-AfterWard-Request':'1','X-AfterWard-CSRF':state.csrf,'X-AfterWard-Revision':String(state.revision),'X-AfterWard-Filename':encodeURIComponent(file.name)},body:file});const value=await response.json();if(!response.ok)throw new Error(errorMessage(value));state=value;const upload=state.uploads.at(-1);location.hash='upload-'+upload.id;render();
      if(config.ocr_provider==='amd_api')for(const p of upload.pages){notice(`Gemma via AMD API: reading page ${p.number} of ${upload.pages.length}…`);await readPage(upload.id,p.number);}
      notice('PDF read. Compare the text with every page.');
    }else if(form.id==='confirm-upload')await mutate('confirm_upload',{upload_id:form.dataset.upload,pages:[...form.querySelectorAll('[name="page"]')].map(el=>el.value),reviewed:form.elements.reviewed.checked},'Document added with reviewed text.','reader-pdf-'+form.dataset.upload,state.uploads.find(u=>u.id===form.dataset.upload).pages.map(p=>`upload-${form.dataset.upload}-${p.number}`));
  }catch(error){notice(error.message);const button=form.querySelector('button');if(button)button.disabled=false;}finally{busy=false;}
});
window.addEventListener('hashchange',()=>{if(page()==='main'){document.querySelector('#main')?.focus();return;}remember();render();document.querySelector('#main')?.scrollIntoView({block:'start'});});
window.addEventListener('beforeunload',remember);
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
  try{const latest=await request(role);if(latest.csrf===state.csrf&&latest.revision>state.revision&&!busy
      &&!document.querySelector('#confirm').open&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){
      remember();state=latest;render();notice('The conversation was updated.');}}
  catch(error){const control=app.querySelector('[data-action="refresh"]');if(control)control.textContent='Check connection: refresh';}
  finally{polling=false;}
}
setInterval(refreshWhenIdle,3000);
window.addEventListener('focus',refreshWhenIdle);
document.addEventListener('visibilitychange',refreshWhenIdle);
