/**
 * job.js -- shared submit/poll/render logic for every Curveball Tools web
 * form. Each tool page has identical structure:
 *   <form id="runForm" data-run-url="/api/<slug>/run"> ... </form>
 *   #statusPanel (.msg, .warning)  -- shown while running
 *   #reportPanel (a.download, pre) -- shown when done
 *   #formError                     -- shown on validation/submit errors
 *
 * initJobForm() wires the form up. One tool = one call, at the bottom of
 * that tool's template. Pass a second `panelSuffix` argument when a page
 * embeds more than one independent job form (e.g. the DS1 Test Builder's
 * build form + GSX Monitor form) so each gets its own status/report panel
 * (statusPanel_<suffix> / reportPanel_<suffix>) instead of sharing one --
 * see _job_panels.html.
 */
function initJobForm(formId, panelSuffix) {
  const form = document.getElementById(formId);
  if (!form) return;
  const runUrl = form.dataset.runUrl;
  const sfx = panelSuffix ? ('_' + panelSuffix) : '';

  // Error div lives inside the form when present (supports multiple forms
  // per page, e.g. the DS1 Test Builder's run + monitor forms).
  const formError = form.querySelector('.field-error') || document.getElementById('formError');
  const statusPanel = document.getElementById('statusPanel' + sfx);
  const statusMsg = statusPanel.querySelector('.msg');
  const statusWarning = statusPanel.querySelector('.warning');
  const statusLog = statusPanel.querySelector('.log');
  let currentJobId = null;
  const reportPanel = document.getElementById('reportPanel' + sfx);
  const reportPre = reportPanel.querySelector('pre.script-pre');
  const precheckBox = reportPanel.querySelector('.precheck-box');
  const precheckPre = reportPanel.querySelector('.precheck-pre');
  const downloadLink = reportPanel.querySelector('a.download');
  const verdictBadge = reportPanel.querySelector('.verdict-badge');
  let pollTimer = null;
  let loggedLines = 0;

  // If the report text has a "STEP 1 -- PRECHECKS ... STEP 2" block (the
  // DS1 Test Builder's shape -- live read-only precheck output embedded
  // ahead of the actual copy/paste script), pull it out into its own
  // collapsed <details> box so it doesn't crowd out the script. Any other
  // report shape (every other tool) just falls through unchanged.
  function renderReport(text) {
    const m = text.match(
      /\n?=+\n\s*STEP 1[^\n]*PRECHECK[^\n]*\n=+\n([\s\S]*?)(?=\n=+\n\s*STEP 2\b)/i);
    if (!m || !m[1].trim()) {
      precheckBox.style.display = 'none';
      reportPre.textContent = text;
      return;
    }
    precheckPre.textContent = m[1].trim();
    precheckBox.style.display = 'block';
    reportPre.textContent = (text.slice(0, m.index) + text.slice(m.index + m[0].length))
      .replace(/\n{3,}/g, '\n\n');
  }

  function formatElapsed(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m > 0 ? (m + 'm ' + s + 's') : (s + 's');
  }

  // ---- JIRA-result notification -------------------------------------------
  // When the live log shows the JIRA post result, get the user's attention
  // so they know the script/report is on the ticket and can continue.
  // Primary signal is a full-width top banner + audible chime -- native OS
  // desktop notifications are subject to Focus Assist / quiet-notification
  // policies outside our control, so they're a bonus, not the main path.
  let jiraNotified = false;
  const JIRA_BASE_URL = 'https://bandwidth-jira.atlassian.net';

  // AudioContext must be created/resumed from a real user gesture (browser
  // autoplay policy) -- do it on form submit (a click), then reuse it later
  // when the async JIRA result arrives.
  let audioCtx = null;
  function unlockAudio() {
    if (audioCtx) return;
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (e) { /* no Web Audio support -- chime just won't play */ }
  }
  function chime(freqs) {
    if (!audioCtx) return;
    try {
      if (audioCtx.state === 'suspended') audioCtx.resume();
      let t = audioCtx.currentTime;
      for (const f of freqs) {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.value = f;
        gain.gain.setValueAtTime(0.18, t);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(t);
        osc.stop(t + 0.3);
        t += 0.16;
      }
    } catch (e) { /* non-fatal -- visual banner still shows */ }
  }

  function showToast(headline, sub, ok) {
    let t = document.getElementById('cbToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'cbToast';
      document.body.appendChild(t);
      t.addEventListener('click', (e) => {
        if (e.target.tagName !== 'A') t.style.display = 'none';
      });
    }
    t.className = ok ? 'ok' : 'err';
    t.innerHTML = '';
    const close = document.createElement('span');
    close.className = 'cbToastClose';
    close.textContent = '✕';
    t.appendChild(close);
    t.appendChild(document.createTextNode(headline));
    const subEl = document.createElement('span');
    subEl.className = 'cbToastSub';
    const m = sub.match(/\b([A-Z]{2,10}-\d+)\b/);
    if (m) {
      const link = document.createElement('a');
      link.className = 'cbToastLink';
      link.href = JIRA_BASE_URL + '/browse/' + m[1];
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open ' + m[1];
      subEl.appendChild(document.createTextNode(sub + '  --  '));
      subEl.appendChild(link);
    } else {
      subEl.textContent = sub;
    }
    t.appendChild(subEl);
    t.style.display = 'block';
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(() => { t.style.display = 'none'; }, 20000);
  }

  function notifyJira(msg, ok) {
    if (jiraNotified) return;
    jiraNotified = true;
    const headline = ok ? 'Posted to JIRA -- ready to continue' : 'JIRA post FAILED';
    if ('Notification' in window && Notification.permission === 'granted') {
      try { new Notification('Curveball Tools -- ' + headline, { body: msg }); } catch (e) {}
    }
    showToast(headline, msg, ok);
    chime(ok ? [880, 1175] : [440, 330]);
    if (!ok) alert('Curveball Tools -- ' + headline + '\n\n' + msg);
  }

  function scanForJiraResult(lines) {
    for (const ln of lines) {
      const s = (ln || '').trim();
      if (/^\[JIRA\] FAILED/.test(s)) { notifyJira(s, false); return; }
      if (/^\[JIRA\] (Comment posted|Comment too large -- attached|File attached)/.test(s)) {
        notifyJira(s, true);
        return;
      }
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    unlockAudio();   // must happen inside this click handler, before any await
    formError.textContent = '';
    if (pollTimer) clearInterval(pollTimer);
    reportPanel.style.display = 'none';
    verdictBadge.style.display = 'none';
    statusPanel.style.display = 'block';
    statusWarning.style.display = 'none';
    statusLog.style.display = 'none';
    statusLog.textContent = '';
    loggedLines = 0;
    jiraNotified = false;
    statusMsg.textContent = 'Submitting...';

    // Ask for desktop-notification permission on first use, so the JIRA
    // "posted" alert can reach the user even from another tab/window.
    if ('Notification' in window && Notification.permission === 'default') {
      try { Notification.requestPermission(); } catch (e) {}
    }

    const fd = new FormData(form);
    let res, data;
    try {
      res = await fetch(runUrl, { method: 'POST', body: fd });
      data = await res.json();
    } catch (err) {
      statusPanel.style.display = 'none';
      formError.textContent = 'Could not reach the server: ' + err;
      return;
    }
    if (!res.ok) {
      statusPanel.style.display = 'none';
      formError.textContent = data.error || 'Unknown error.';
      return;
    }

    const jobId = data.job_id;
    currentJobId = jobId;
    pollTimer = setInterval(() => pollStatus(jobId), 2000);
    pollStatus(jobId);
  });

  function appendLog(lines) {
    if (!lines || lines.length <= loggedLines) return;
    const newLines = lines.slice(loggedLines);
    loggedLines = lines.length;
    if (newLines.length) {
      statusLog.style.display = 'block';
      statusLog.textContent += (statusLog.textContent ? '\n' : '') + newLines.join('\n');
      statusLog.scrollTop = statusLog.scrollHeight;
      scanForJiraResult(newLines);
    }
  }

  async function pollStatus(jobId) {
    let res, data;
    try {
      res = await fetch('/api/job/' + jobId);
      data = await res.json();
    } catch (err) {
      statusMsg.textContent = 'Lost contact with the server -- it may still be running. Retrying...';
      return;
    }

    // The 404 body ({"error": "Unknown job id."}) has no `status` field, so
    // it fell through every branch below silently -- the poller kept hitting
    // a dead job id every 2s forever with the UI frozen on stale "Running...".
    // Most common cause: the dev server's file-watcher reloaded mid-run
    // (e.g. a code change saved while a job was active), which wipes the
    // in-memory job registry. Surfaced explicitly now instead of silently
    // polling nothing (confirmed live 2026-09-03).
    if (!res.ok) {
      clearInterval(pollTimer);
      statusMsg.textContent = 'Lost this job -- the server likely restarted while it was running ' +
        '(' + (data.error || ('HTTP ' + res.status)) + '). Please re-run.';
      statusWarning.style.display = 'none';
      return;
    }

    appendLog(data.log);

    // Page-specific hook: lets a template react to job data each poll
    // (e.g. the DS1 Test Builder auto-fills its GSX Monitor form from
    // data.extras.gsx_targets).
    if (window.onJobData) {
      try { window.onJobData(data); } catch (e) { /* non-fatal */ }
    }

    if (data.status === 'running') {
      statusMsg.textContent = 'Running' + (data.label ? (' -- ' + data.label) : '') +
        '... elapsed ' + formatElapsed(data.elapsed);
      statusWarning.style.display = data.warning ? 'block' : 'none';
    } else if (data.status === 'done') {
      clearInterval(pollTimer);
      statusMsg.textContent = 'Done in ' + formatElapsed(data.elapsed) + '.';
      statusWarning.style.display = 'none';
      reportPanel.style.display = 'block';
      renderReport(data.report);
      downloadLink.href = data.download_url;
      if (data.verdict) {
        const flagged = data.verdict.flagged || [];
        verdictBadge.style.display = 'inline-block';
        if (data.verdict.verdict === 'clean') {
          verdictBadge.className = 'verdict-badge ok';
          verdictBadge.textContent = 'PASS -- ' + data.verdict.checked + ' DS1(s) clean';
        } else {
          verdictBadge.className = 'verdict-badge fail';
          verdictBadge.textContent = 'ISSUES DETECTED -- ' + flagged.join(', ');
        }
      } else {
        verdictBadge.style.display = 'none';
      }
    } else if (data.status === 'error') {
      clearInterval(pollTimer);
      statusMsg.textContent = 'Error after ' + formatElapsed(data.elapsed) + ': ' + data.error;
      statusWarning.style.display = 'none';
    }
  }
}

/** Shared JIRA-checkbox toggle: shows/hides the ticket field. */
function initJiraToggle(checkboxId, wrapId) {
  const cb = document.getElementById(checkboxId);
  const wrap = document.getElementById(wrapId);
  if (!cb || !wrap) return;
  cb.addEventListener('change', (e) => {
    wrap.style.display = e.target.checked ? 'block' : 'none';
  });
}
