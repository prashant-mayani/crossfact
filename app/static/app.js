/* Crossfact client. No framework, no inline scripts (the CSP forbids them). */
(function () {
  'use strict';
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  const REASONS = {
    side_a_failed: 'Only side B answered; side A failed or was too slow. This is a single-model answer, not a consensus.',
    side_b_failed: 'Only side A answered; side B failed or was too slow. This is a single-model answer, not a consensus.',
    comparator_failed: 'Both models answered, but comparing them failed, so both answers are shown in full.',
    judge_failed: 'The models still disagreed and no judge was available, so both positions are shown.',
    deadline: 'Stopped early to stay inside the time limit; anything unfinished is shown as it stood.',
    merge_failed: 'Merging failed, so both answers are shown in full.',
  };
  const CHIP = {abandoned: 'interrupted', error: 'failed'};

  // ---- every page: local times, confirm prompts ---------------------------------
  $$('time[data-ts]').forEach((t) => {
    const d = new Date(Number(t.dataset.ts) * 1000);
    t.dateTime = d.toISOString();
    t.textContent = d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
  });
  $$('form[data-confirm]').forEach((f) => f.addEventListener('submit', (e) => {
    if (!window.confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // ---- files page ------------------------------------------------------------
  const upform = $('#upform');
  if (upform) {
    upform.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = $('#upmsg');
      msg.textContent = 'Uploading…';
      const r = await fetch('/files/upload', {method: 'POST', body: new FormData(upform)});
      if (r.ok) { location.reload(); return; }
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
      msg.textContent = 'Upload failed: ' + detail;
    });
  }

  // ---- chat page -----------------------------------------------------------------
  const composer = $('#composer');
  const bootEl = $('#boot');
  if (!composer || !bootEl) return;
  const BOOT = JSON.parse(bootEl.textContent);
  const PROVIDERS = BOOT.providers;
  const formerr = $('#formerr');

  function fillProviders(side) {
    const psel = $(`select[name="${side}_provider"]`);
    psel.innerHTML = PROVIDERS.map((p) => {
      const note = (p.trains ? ' · may train on data' : '') + (p.ok ? '' : ' · no key');
      return `<option value="${esc(p.id)}" ${p.ok ? '' : 'disabled'}>${esc(p.label + note)}</option>`;
    }).join('');
    const def = BOOT.defaults[side === 'a' ? 'side_a' : 'side_b'] || {};
    const usable = PROVIDERS.filter((p) => p.ok);
    if (def.provider && usable.some((p) => p.id === def.provider)) psel.value = def.provider;
    else if (usable[0]) psel.value = usable[0].id;
    fillModels(side, def.model);
    if (def.effort) $(`select[name="${side}_effort"]`).value = def.effort;
    psel.addEventListener('change', () => fillModels(side));
  }
  function fillModels(side, prefer) {
    const pid = $(`select[name="${side}_provider"]`).value;
    const p = PROVIDERS.find((x) => x.id === pid) || {models: [], open: []};
    $(`select[name="${side}_model"]`).innerHTML = p.models.map((m) => {
      const paused = (p.open || []).includes(m.id) ? ' · paused after failures' : '';
      return `<option value="${esc(m.id)}">${esc(m.label + paused)}</option>`;
    }).join('');
    if (prefer && p.models.some((m) => m.id === prefer)) $(`select[name="${side}_model"]`).value = prefer;
  }
  fillProviders('a');
  fillProviders('b');
  const modeSel = $('select[name="mode"]');
  const syncMode = () => $('.side.b').classList.toggle('off', modeSel.value === 'single');
  modeSel.addEventListener('change', syncMode);
  syncMode();

  // ---- rendering ---------------------------------------------------------------------
  function stageHtml(st) {
    let head = st.stage;
    if (st.side) head += ` · side ${st.side}`;
    if (st.provider) head += ` · ${st.provider}/${st.model} (${st.effort})`;
    if (st.round) head += ` · round ${st.round}`;
    if (st.attempts > 1) head += ` · ${st.attempts} attempts`;
    if (st.stage === 'answer' && st.duration_s != null) head += ` · ${st.duration_s}s`;
    if (st.t != null) head += ` · t+${st.t}s`;
    let body = '';
    if (st.stage === 'answer') {
      if (st.substituted_from) body += `<div class="sub">Stood in for ${esc(st.substituted_from)}, which failed.</div>`;
      body += `<pre>${esc(st.text)}</pre>`;
    } else if (st.stage === 'comparator') {
      const n = (k) => (st[k] || []).length;
      body = `<div class="cmp"><b>verdict:</b> ${esc(st.verdict)} · <b>agreements:</b> ${n('agreements')} · ` +
        `<b>only A:</b> ${n('unique_a')} · <b>only B:</b> ${n('unique_b')} · <b>conflicts:</b> ${n('conflicts')}</div>`;
      body += (st.conflicts || []).map((c) =>
        `<pre class="conflict">⚡ ${esc(c.topic)}\nA: ${esc(c.a_position)}\nB: ${esc(c.b_position)}</pre>`).join('');
    } else if (st.stage === 'debate') {
      body = `<pre>A: ${esc(st.a)}\n\nB: ${esc(st.b)}</pre>`;
    } else if (st.stage === 'judge') {
      body = (st.rulings || []).map((r) =>
        `<pre class="ruling">⚖ ${esc(r.topic)} → ${esc(r.winner)}: ${esc(r.reason)}</pre>`).join('') +
        `<pre>${esc(st.analysis)}</pre>`;
    } else if (st.stage === 'error') {
      body = `<pre class="err">${esc(st.where)}${st.model ? ' (' + esc(st.model) + ')' : ''}` +
        `${st.kind ? ' · ' + esc(st.kind) : ''}: ${esc(st.detail)}</pre>`;
    } else if (st.stage === 'note') {
      body = `<pre>${esc(st.detail)}</pre>`;
    } else if (st.stage === 'final') {
      body = `<div class="cmp">outcome ${esc(st.outcome)}${st.duration_s != null ? ' in ' + st.duration_s + 's' : ''}</div>`;
    }
    return `<div class="stage ${esc(st.stage)}"><div class="stagehead">${esc(head)}</div>${body}</div>`;
  }

  function renderAssistant(el, data) {
    const pl = data.pipeline || {stages: []};
    const stages = pl.stages || [];
    const oc = pl.outcome || '';
    el.querySelector('.verdictbar').className = 'verdictbar ' + oc;
    const chip = oc ? `<span class="chip ${esc(oc)}">${esc(CHIP[oc] || oc)}</span>` : '';
    const banners = (pl.degraded || []).map((r) => `<p class="banner">${esc(REASONS[r] || r)}</p>`).join('');
    const last = stages[stages.length - 1];
    const running = data.status === 'running'
      ? `<div class="running">${last ? 'working: ' + esc(last.stage) : 'starting'}<span class="dots"></span></div>` : '';
    const retry = (data.status === 'error' || oc === 'degraded' || oc === 'abandoned')
      ? '<button class="retry" type="button">Retry</button>' : '';
    const details = stages.length
      ? `<details class="pipeline" ${data.status === 'running' ? 'open' : ''}><summary>Pipeline transcript ` +
        `(${stages.length} stages)</summary><div class="stages">${stages.map(stageHtml).join('')}</div></details>` : '';
    el.querySelector('.bubble').innerHTML =
      `${chip}${banners}<div class="content">${esc(data.content)}</div>${running}${retry}${details}`;
    if (pl.sides) el.dataset.sides = JSON.stringify(pl.sides);
    if (pl.mode) el.dataset.mode = pl.mode;
  }

  function addMsg(cls, html) {
    const d = document.createElement('div');
    d.className = 'msg ' + cls;
    d.innerHTML = html;
    $('#msgs').appendChild(d);
    d.scrollIntoView({block: 'end'});
    return d;
  }

  function poll(mid, el) {
    let failures = 0;
    const timer = setInterval(async () => {
      let r;
      try { r = await fetch(`/api/messages/${mid}`, {credentials: 'same-origin'}); } catch (_) { r = null; }
      if (!r || !r.ok) {
        // A deploy can make one poll miss; give up only after a sustained outage.
        if (++failures > 24) clearInterval(timer);
        return;
      }
      failures = 0;
      const data = await r.json();
      renderAssistant(el, data);
      if (data.status !== 'running') clearInterval(timer);
    }, 2500);
  }
  $$('.msg.assistant[data-status="running"]').forEach((el) => poll(el.dataset.mid, el));

  // ---- submit & retry ------------------------------------------------------------------
  async function submit(fd, questionText) {
    fd.set('csrf', BOOT.csrf);
    formerr.textContent = '';
    $('#send').disabled = true;
    let r;
    try { r = await fetch('/ask', {method: 'POST', body: fd}); } finally { $('#send').disabled = false; }
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (_) { /* not JSON */ }
      formerr.textContent = detail;
      return;
    }
    const {thread_id: tid, message_id: mid} = await r.json();
    if (!fd.get('thread_id')) { location.href = `/chat/${tid}`; return; }
    addMsg('user', `<div class="bubble">${esc(questionText)}</div>`);
    const el = addMsg('assistant', '<div class="verdictbar"></div><div class="bubble"><div class="content"></div>' +
      '<div class="running">starting<span class="dots"></span></div></div>');
    el.dataset.mid = mid;
    $('#q').value = '';
    poll(mid, el);
  }

  composer.addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('#q').value.trim();
    if (q) submit(new FormData(composer), q);
  });
  $('#q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && window.innerWidth > 700) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });

  $('#msgs').addEventListener('click', (e) => {
    const btn = e.target.closest('button.retry');
    if (!btn) return;
    const el = btn.closest('.msg.assistant');
    let sides = {};
    try { sides = JSON.parse(el.dataset.sides || '{}'); } catch (_) { /* keep defaults */ }
    const question = el.dataset.question || (el.previousElementSibling && el.previousElementSibling.textContent) || '';
    if (!question.trim()) return;
    const fd = new FormData();
    fd.set('question', question.trim());
    fd.set('thread_id', composer.querySelector('[name="thread_id"]').value);
    fd.set('mode', el.dataset.mode || 'consensus');
    fd.set('file_ids', el.dataset.files || '');
    for (const s of ['A', 'B']) {
      const cfg = sides[s] || {};
      const k = s.toLowerCase();
      fd.set(`${k}_provider`, cfg.provider || $(`select[name="${k}_provider"]`).value);
      fd.set(`${k}_model`, cfg.model || $(`select[name="${k}_model"]`).value);
      fd.set(`${k}_effort`, cfg.effort || $(`select[name="${k}_effort"]`).value);
    }
    btn.disabled = true;
    submit(fd, question.trim());
  });
})();
