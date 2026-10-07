(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var stage     = $('stage');
  var list      = $('list');
  var fileInput = $('file');
  var fileName  = $('fileName');
  var fileMeta  = $('fileMeta');
  var errorEl   = $('error');
  var presetEl  = $('preset');
  var stripEl   = $('strip');
  var dlAll     = $('downloadAll');
  var tpl       = $('cardTpl');

  var PRESET_LABEL = { lossless: 'Lossless', balanced: 'Balanced', max: 'Max', screen: 'Screen' };

  // One item per file. Results are cached per settings, so switching the
  // preset back and forth doesn't redo work.
  //   it.results[key] = { jobId, state, report, error }
  var items = [];

  var ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" ' +
                'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var ICON_BAD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" ' +
                 'stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  // ============================================================= helpers ===

  function human(n) {
    if (n < 1024) return n + ' B';
    var u = ['KB', 'MB', 'GB'], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function q(el, sel) { return el.querySelector(sel); }

  function settings() { return { preset: presetEl.value, strip: stripEl.checked }; }
  function keyOf(st) { return st.preset + (st.strip ? '' : '|keep'); }
  function cur(it) { return it.results[it.key]; }
  function label(st) { return PRESET_LABEL[st.preset] + (st.strip ? '' : ' · private data kept'); }
  function downloadable(res) { return res && res.state === 'done' && res.report.outcome === 'compressed'; }

  // ============================================================== header ===

  function updateSummary() {
    stage.classList.toggle('empty', items.length === 0);
    if (!items.length) {
      fileName.textContent = 'No files added';
      fileName.classList.add('empty');
      fileMeta.textContent = '';
      dlAll.disabled = true;
      return;
    }
    var before = 0, after = 0, ready = 0, done = 0, busy = 0;
    items.forEach(function (it) {
      var res = cur(it);
      if (res && res.state === 'done') {
        done++;
        before += res.report.original_size;
        after += res.report.final_size;
        if (res.report.outcome === 'compressed') ready++;
      } else if (!res || res.state === 'queued' || res.state === 'running') {
        busy++;
      }
    });
    fileName.classList.remove('empty');
    fileName.textContent = items.length + (items.length === 1 ? ' file' : ' files') + ' · ' + label(settings());
    fileMeta.textContent = done
      ? human(before) + ' → ' + human(after) + ' · ' +
        (before ? Math.round(100 * (before - after) / before) : 0) + '% saved' +
        (busy ? ' · ' + busy + ' in progress' : '')
      : busy + ' in progress';
    dlAll.disabled = ready === 0;
  }

  // =============================================================== cards ===

  function setBadge(it, cls, text) {
    var b = q(it.el, '.badge');
    b.className = 'badge' + (cls ? ' ' + cls : '');
    b.textContent = text;
  }

  // Render whatever the item's current-settings result is.
  function render(it) {
    var res = cur(it), el = it.el, st = it.settings;
    q(el, '.download').disabled = !downloadable(res);
    q(el, '.download').title = downloadable(res) ? 'Download the ' + label(st) + ' version' : '';
    q(el, '.preview').disabled = !(res && res.state === 'done');
    q(el, '.toggle').disabled = !(res && (res.state === 'done' || res.state === 'error'));
    q(el, '.card-saved').className = 'card-saved';
    q(el, '.card-saved').textContent = '';
    q(el, '.card-sizes').textContent = human(it.file.size);
    q(el, '.card-meta').textContent = label(st);
    if (!res || res.state === 'queued' || res.state === 'running') {
      el.dataset.state = res ? res.state : 'queued';
      var pct = res ? Math.round((res.progress || 0) * 100) : 0;
      q(el, '.bar > div').style.width = pct + '%';
      setBadge(it, res && res.state === 'running' ? 'running' : '',
               res && res.state === 'running' ? res.stage + (pct ? ' ' + pct + '%' : '') : 'Queued');
      q(el, '.card-detail').innerHTML = '';
      return;
    }
    if (res.state === 'error') {
      el.dataset.state = 'error';
      setBadge(it, 'fail', 'Error');
      q(el, '.card-detail').innerHTML = '<div class="note err">' + esc(res.error) + '</div>';
      return;
    }
    var r = res.report;
    el.dataset.state = 'done';
    var pages = r.verification.pages.length;
    q(el, '.card-meta').textContent = pages + (pages === 1 ? ' page · ' : ' pages · ') +
      label(st) + ' · ' + r.elapsed_s + ' s';
    if (r.outcome === 'compressed') {
      setBadge(it, 'pass', r.fallback ? 'Verified · lossless only' : 'Verified');
      q(el, '.card-sizes').textContent = human(r.original_size) + ' → ' + human(r.final_size);
      q(el, '.card-saved').textContent = '−' + r.saved_pct + '%';
      q(el, '.card-saved').className = 'card-saved good';
    } else {
      var failed = !r.verification.passed;
      setBadge(it, failed ? 'fail' : 'kept', failed ? 'Original kept · failed checks' : 'Original kept');
      q(el, '.card-sizes').textContent = human(r.original_size);
      q(el, '.card-saved').textContent = r.candidate_saved_pct > 0 ? '−' + r.candidate_saved_pct + '%' : '0%';
    }
    q(el, '.card-detail').innerHTML = reportHTML(r);
  }

  function reportHTML(r) {
    var v = r.verification, h = [];

    if (r.outcome_reason) h.push('<div class="note warn">' + esc(r.outcome_reason) + '</div>');
    if (r.fallback) h.push('<div class="note warn">' + esc(r.fallback) + '</div>');

    // Summary numbers
    var rec = r.images.filter(function (i) { return i.action === 'recompressed'; });
    var s = r.structural || {};
    h.push('<div class="section"><div class="stats">' +
      stat(human(r.original_size), 'Original') +
      stat(human(r.candidate_size), 'Compressed') +
      stat(r.candidate_saved_pct + '%', 'Saved') +
      stat(rec.length + ' / ' + r.images.length, 'Images recompressed') +
      stat(s.duplicates_merged || 0, 'Duplicate objects merged') +
      stat((s.pieceinfo || 0) + (s.object_xmp || 0) + (s.thumbnails_removed || 0), 'Private data / thumbnails removed') +
      '</div></div>');

    // Verification
    var passed = v.pages.filter(function (p) { return p.passed; }).length;
    var identical = v.pages.filter(function (p) { return p.identical; }).length;
    h.push('<div class="section"><h3>Verification <span>' + passed + '/' + v.pages.length +
      ' pages pass · ' + identical + ' pixel-identical · ' +
      (v.thresholds.mode === 'screen' ? 'screen-viewing check · ' : '') + v.thresholds.dpi + ' dpi RGB' +
      (v.cmyk_checked ? ' + CMYK' : '') + ' · SSIM ≥ ' + v.thresholds.ssim_page_min +
      ', ΔE₀₀ mean &lt; ' + v.thresholds.de_mean_max + ', region ≤ ' + v.thresholds.de_region_max +
      '</span></h3>');
    h.push('<div class="chips" style="margin-bottom:8px">' + v.doc_checks.map(function (c) {
      return '<span class="chip ' + (c.ok ? 'ok' : 'bad') + '">' + (c.ok ? ICON_OK : ICON_BAD) + esc(c.check) + '</span>';
    }).join('') + '</div>');
    h.push('<div class="table-wrap"><table><thead><tr><th>Page</th><th>Result</th><th>SSIM</th>' +
      '<th>Min region</th><th>ΔE mean</th><th>ΔE region</th>' + (v.cmyk_checked ? '<th>CMYK SSIM</th>' : '') +
      '<th>Notes</th></tr></thead><tbody>');
    v.pages.forEach(function (p) {
      var notes = p.identical ? '<span class="muted">Pixel-identical</span>'
        : esc(p.structure_failures.concat(p.visual_failures).join('; ')) || '<span class="muted">Within tolerance</span>';
      h.push('<tr><td class="num">' + p.page + '</td><td>' +
        (p.passed ? '<span class="ok">Pass</span>' : '<span class="bad">Fail</span>') + '</td>' +
        '<td class="num">' + p.ssim.toFixed(5) + '</td><td class="num">' + p.ssim_tile_min.toFixed(4) + '</td>' +
        '<td class="num">' + p.de_mean.toFixed(3) + '</td><td class="num">' + p.de_tile_max.toFixed(2) + '</td>' +
        (v.cmyk_checked ? '<td class="num">' + (p.cmyk_ssim == null ? '—' : p.cmyk_ssim.toFixed(5)) + '</td>' : '') +
        '<td class="wrap">' + notes + '</td></tr>');
    });
    h.push('</tbody></table></div></div>');

    // Retries
    var retried = r.attempts.filter(function (a) { return !a.passed || a.reverted.length; });
    if (retried.length) {
      h.push('<div class="section"><h3>Revert rounds</h3><div class="table-wrap"><table><thead><tr>' +
        '<th>Round</th><th>Result</th><th>Failing pages</th><th>Action</th><th>Why</th></tr></thead><tbody>');
      r.attempts.forEach(function (a) {
        h.push('<tr><td class="num">' + esc(a.round) + '</td><td>' +
          (a.passed ? '<span class="ok">Pass</span>' : '<span class="bad">Fail</span>') + '</td>' +
          '<td class="num">' + (a.failing_pages.join(', ') || '—') + '</td>' +
          '<td class="mono">' + actionText(a) + '</td>' +
          '<td class="wrap">' + esc((a.failures || []).join(' · ')) + '</td></tr>');
      });
      h.push('</tbody></table></div></div>');
    }

    // Images
    if (r.images.length) {
      var rows = r.images.slice().sort(function (a, b) {
        var o = { recompressed: 0, reverted: 1, skipped: 2 };
        return (o[a.action] - o[b.action]) || (b.bytes_before - a.bytes_before);
      });
      h.push('<div class="section"><h3>Images <span>' + rec.length + ' recompressed · ' +
        (r.images.length - rec.length) + ' left untouched</span></h3><div class="table-wrap"><table><thead><tr>' +
        '<th>Object</th><th>Pages</th><th>Pixels</th><th>Colour</th><th>ppi</th><th>Size</th><th>Result</th></tr></thead><tbody>');
      rows.forEach(function (i) {
        var px = i.width + '×' + i.height + (i.new_width && (i.new_width !== i.width) ? ' → ' + i.new_width + '×' + i.new_height : '');
        var size = human(i.bytes_before) + (i.bytes_after != null ? ' → ' + human(i.bytes_after) : '');
        var res = i.action === 'recompressed' ? '<span class="ok">Recompressed</span> · '
          : i.action === 'reverted' ? '<span class="bad">Reverted</span> · ' : '<span class="muted">Skipped</span> · ';
        h.push('<tr><td class="mono">' + esc(i.id) + '</td><td class="num">' + (i.pages.join(', ') || '—') + '</td>' +
          '<td class="num">' + px + '</td><td class="mono">' + esc(i.colorspace) + '</td>' +
          '<td class="num">' + (i.ppi == null ? '—' : Math.round(i.ppi)) + '</td><td class="num">' + size + '</td>' +
          '<td class="wrap">' + res + esc(i.reason) + '</td></tr>');
      });
      h.push('</tbody></table></div></div>');
    }
    return h.join('');
  }

  function actionText(a) {
    var parts = [];
    if (a.eased && a.eased.length) parts.push('Eased to Balanced: ' + a.eased.join(', '));
    if (a.reverted.length) parts.push('Reverted: ' + a.reverted.join(', '));
    return esc(parts.join(' · ')) || '—';
  }

  function stat(value, label) {
    return '<div class="stat"><b>' + esc(value) + '</b><small>' + esc(label) + '</small></div>';
  }

  // ================================================================ jobs ===

  function addFiles(files) {
    errorEl.textContent = '';
    var pdfs = Array.prototype.filter.call(files, function (f) {
      return /\.pdf$/i.test(f.name) || f.type === 'application/pdf';
    });
    if (!pdfs.length) {
      errorEl.textContent = 'Only PDF files can be compressed.';
      return;
    }
    pdfs.forEach(function (f) {
      var el = tpl.content.firstElementChild.cloneNode(true);
      var it = { file: f, el: el, results: {}, uploadJob: null };
      q(el, '.card-name').textContent = f.name;
      q(el, '.card-name').title = f.name;
      q(el, '.download').addEventListener('click', function () { download(it); });
      q(el, '.preview').addEventListener('click', function () { openPreview(it); });
      q(el, '.rerun').addEventListener('click', function () { delete it.results[it.key]; run(it); });
      q(el, '.toggle').addEventListener('click', function () {
        var open = el.classList.toggle('open');
        q(el, '.card-detail').hidden = !open;
        q(el, '.toggle').setAttribute('aria-expanded', open);
      });
      list.appendChild(el);
      items.push(it);
      run(it);
    });
    updateSummary();
  }

  // Show the result for the current settings, starting a job if there isn't one.
  function run(it) {
    var st = settings(), key = keyOf(st);
    var prev = cur(it);
    if (prev && prev !== it.results[key] && (prev.state === 'queued' || prev.state === 'running')) {
      fetch('/api/jobs/' + prev.jobId, { method: 'DELETE' });   // stop work nobody will see
      delete it.results[it.key];
    }
    it.key = key;
    it.settings = st;
    if (!it.results[key]) {
      var res = it.results[key] = { state: 'queued', progress: 0 };
      var url = '/api/jobs?preset=' + st.preset + '&strip=' + (st.strip ? 1 : 0);
      var opts = { method: 'POST' };
      if (it.uploadJob) {
        url += '&from=' + it.uploadJob;                       // reuse the upload
      } else {
        url += '&name=' + encodeURIComponent(it.file.name);
        opts.body = it.file;
        opts.headers = { 'Content-Type': 'application/pdf' };
      }
      fetch(url, opts)
        .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); })
        .then(function (j) {
          if (!it.uploadJob) it.uploadJob = j.id;
          res.jobId = j.id;
          poll(it, res);
        })
        .catch(function (e) { res.state = 'error'; res.error = e.message; refresh(it, res); });
    }
    render(it);
    updateSummary();
  }

  function refresh(it, res) {
    if (cur(it) === res) { render(it); updateSummary(); }
  }

  function poll(it, res) {
    fetch('/api/jobs/' + res.jobId).then(function (r) { return r.json(); }).then(function (job) {
      if (job.state === 'cancelled') return;
      res.state = job.state;
      res.stage = job.stage;
      res.progress = job.progress;
      if (job.state === 'done') res.report = job.report;
      if (job.state === 'error') res.error = job.error;
      refresh(it, res);
      if (job.state === 'queued' || job.state === 'running') {
        setTimeout(function () { poll(it, res); }, 350);
      }
    }).catch(function () { setTimeout(function () { poll(it, res); }, 1000); });
  }

  function onSettingsChange() {
    items.forEach(run);
  }
  presetEl.addEventListener('change', onSettingsChange);
  stripEl.addEventListener('change', onSettingsChange);

  function filenameFor(it) {
    var base = it.file.name.replace(/\.pdf$/i, '');
    return base + '_compressed.pdf';
  }

  function download(it) {
    var res = cur(it);
    if (!downloadable(res)) return;
    var a = document.createElement('a');
    a.href = '/api/jobs/' + res.jobId + '/download';
    a.download = filenameFor(it);
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  dlAll.addEventListener('click', function () {
    var ready = items.filter(function (it) { return downloadable(cur(it)); });
    ready.forEach(function (it, i) { setTimeout(function () { download(it); }, i * 400); });
  });

  // ============================================================= preview ===

  var pv = $('preview'), pvPages = $('pvPages'), pvScroll = $('pvScroll');
  var pvItem = null, pvMode = 'new', pvObserver = null;

  function openPreview(it) {
    var res = cur(it);
    if (!res || res.state !== 'done') return;
    pvItem = it;
    var r = res.report, hasNew = r.outcome === 'compressed';
    $('pvName').textContent = it.file.name;
    $('pvMeta').textContent = label(it.settings) + ' · ' + (hasNew
      ? human(r.original_size) + ' → ' + human(r.final_size) + ' · −' + r.saved_pct + '%'
      : 'original kept, no compressed file');
    q(pv, '[data-mode=new]').disabled = !hasNew;
    q(pv, '[data-mode=both]').disabled = !hasNew;
    $('pvDownload').disabled = !hasNew;
    pv.hidden = false;                       // visible first, so widths can be measured
    document.body.style.overflow = 'hidden';
    setMode(hasNew ? pvMode : 'orig');
    pvScroll.scrollTop = 0;
    $('pvClose').focus();
  }

  function closePreview() {
    pv.hidden = true;
    pvPages.innerHTML = '';
    if (pvObserver) pvObserver.disconnect();
    pvItem = null;
  }

  function setMode(mode, rebuild) {
    pvMode = mode;
    Array.prototype.forEach.call(pv.querySelectorAll('#pvMode button'), function (b) {
      b.classList.toggle('on', b.dataset.mode === mode);
    });
    pv.classList.toggle('both', mode === 'both');
    if (rebuild !== false) buildPages();
  }

  function buildPages() {
    var res = cur(pvItem), r = res.report;
    var checks = {};
    r.verification.pages.forEach(function (p) { checks[p.page] = p; });
    var onlyChanged = $('pvChanged').checked;
    var sides = pvMode === 'both' ? ['orig', 'new'] : [pvMode];
    // Request roughly the on-screen width, sharpened for retina, in steps so
    // the browser cache gets reused when toggling modes.
    var inner = pvPages.clientWidth - 32;
    var colW = pvMode === 'both' ? (inner - 12) / 2 : inner;
    var want = Math.ceil(colW * (window.devicePixelRatio || 1) / 200) * 200;

    if (pvObserver) pvObserver.disconnect();
    pvObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) $('pvPage').textContent = e.target.dataset.page + ' / ' + r.page_sizes.length;
      });
    }, { root: pvScroll, rootMargin: '-45% 0px -45% 0px' });

    var html = [], shown = 0;
    r.page_sizes.forEach(function (sz, i) {
      var n = i + 1, c = checks[n] || {};
      var changed = !c.identical;
      if (onlyChanged && !changed) return;
      shown++;
      var imgs = sides.map(function (side) {
        var job = side === 'orig' ? pvItem.uploadJob : res.jobId;
        return '<div><img loading="lazy" decoding="async" width="' + Math.round(sz[0]) +
          '" height="' + Math.round(sz[1]) + '" alt="Page ' + n + ' (' + (side === 'orig' ? 'original' : 'compressed') +
          ')" src="/api/jobs/' + job + '/page/' + n + '?which=' + side + '&w=' + want + '">' +
          '<span class="tag">' + (side === 'orig' ? 'Original' : 'Compressed') + '</span></div>';
      }).join('');
      var note = c.identical ? 'Pixel-identical to the original'
        : (c.passed ? 'Changed within tolerance · SSIM ' + (c.ssim || 0).toFixed(4) + ' · ΔE ' + (c.de_mean || 0).toFixed(2)
                    : 'Failed checks');
      var badge = c.identical ? '' : (c.passed ? '<span class="badge running">Changed</span>' : '<span class="badge fail">Failed</span>');
      html.push('<figure class="pv-page" data-page="' + n + '"><div class="pv-imgs">' + imgs + '</div>' +
        '<figcaption><span class="num">Page ' + n + '</span>' + badge + '<span>' + esc(note) + '</span></figcaption></figure>');
    });
    pvPages.innerHTML = shown ? html.join('') : '<div class="pv-empty">Every page is pixel-identical to the original.</div>';
    Array.prototype.forEach.call(pvPages.querySelectorAll('.pv-page'), function (f) { pvObserver.observe(f); });
    $('pvPage').textContent = (shown ? pvPages.querySelector('.pv-page').dataset.page : '—') + ' / ' + r.page_sizes.length;
  }

  Array.prototype.forEach.call(pv.querySelectorAll('#pvMode button'), function (b) {
    b.addEventListener('click', function () { if (!b.disabled) setMode(b.dataset.mode); });
  });
  $('pvChanged').addEventListener('change', function () { buildPages(); pvScroll.scrollTop = 0; });
  $('pvClose').addEventListener('click', closePreview);
  $('pvDownload').addEventListener('click', function () { if (pvItem) download(pvItem); });
  document.addEventListener('keydown', function (e) {
    if (pv.hidden) return;
    if (e.key === 'Escape') { closePreview(); document.body.style.overflow = ''; }
  });
  $('pvClose').addEventListener('click', function () { document.body.style.overflow = ''; });

  // ======================================================== file intake ===

  fileInput.addEventListener('change', function () {
    if (fileInput.files.length) addFiles(fileInput.files);
    fileInput.value = '';
  });

  var dragDepth = 0;
  window.addEventListener('dragenter', function (e) {
    e.preventDefault();
    if (++dragDepth === 1) stage.classList.add('dragover');
  });
  window.addEventListener('dragleave', function (e) {
    e.preventDefault();
    if (--dragDepth <= 0) { dragDepth = 0; stage.classList.remove('dragover'); }
  });
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop', function (e) {
    e.preventDefault();
    dragDepth = 0;
    stage.classList.remove('dragover');
    if (pv.hidden && e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  updateSummary();
})();
