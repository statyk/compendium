// Barcode scanning: native BarcodeDetector (Chromium) with ZXing fallback (Safari/Firefox).
// USB keyboard-wedge scanners work as keyboard input automatically — no JS path required.

(function () {
  'use strict';

  const BEEP_KEY = 'compendium-beep-enabled';

  function playBeep() {
    if (localStorage.getItem(BEEP_KEY) === 'false') return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = 1200;
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.08);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.08);
    } catch (_) {}
  }

  function stopStream(stream) {
    if (stream) stream.getTracks().forEach(t => t.stop());
  }

  async function detectBackend() {
    if (typeof BarcodeDetector !== 'undefined') {
      try {
        const formats = await BarcodeDetector.getSupportedFormats();
        if (formats.some(f => ['ean_13', 'ean_8', 'code_128', 'upc_a'].includes(f))) {
          return 'native';
        }
      } catch (_) {}
    }
    if (window.ZXingBrowser && typeof window.ZXingBrowser.BrowserMultiFormatReader === 'function') {
      return 'zxing';
    }
    return null;
  }

  function startNative(video, onResult) {
    const detector = new BarcodeDetector({
      formats: ['ean_13', 'ean_8', 'code_128', 'code_39', 'upc_a', 'upc_e', 'qr_code'],
    });
    let active = true;

    function tick() {
      if (!active) return;
      detector.detect(video)
        .then(results => {
          if (!active) return;
          if (results.length > 0) {
            active = false;
            onResult(results[0].rawValue);
          } else {
            requestAnimationFrame(tick);
          }
        })
        .catch(() => active && requestAnimationFrame(tick));
    }
    requestAnimationFrame(tick);
    return () => { active = false; };
  }

  async function startZXing(video, onResult) {
    const reader = new ZXingBrowser.BrowserMultiFormatReader();
    let controls = null;
    let done = false;

    try {
      controls = await reader.decodeFromStream(video.srcObject, video, (result) => {
        if (done) return;
        if (result) {
          done = true;
          if (controls) controls.stop();
          onResult(result.getText());
        }
      });
      return () => { done = true; if (controls) controls.stop(); };
    } catch (_) {
      return () => {};
    }
  }

  async function openScanner(targetInput, backend) {
    const dialog = document.getElementById('scanner-dialog');
    const video = document.getElementById('scanner-video');
    const errorEl = document.getElementById('scanner-error');
    const closeBtn = document.getElementById('scanner-close');
    const ac = new AbortController();

    video.hidden = false;
    errorEl.hidden = true;

    let stream = null;
    let stopScan = null;
    let cancelled = false;

    function cleanup() {
      cancelled = true;
      ac.abort();
      if (stopScan) { stopScan(); stopScan = null; }
      stopStream(stream);
      stream = null;
      video.srcObject = null;
    }

    dialog.addEventListener('close', cleanup, { signal: ac.signal });
    closeBtn.addEventListener('click', () => dialog.close(), { signal: ac.signal });
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); }, { signal: ac.signal });

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: 'environment',
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
      });
    } catch (_) {
      video.hidden = true;
      errorEl.hidden = false;
      dialog.showModal();
      return;
    }

    video.srcObject = stream;
    await video.play().catch(() => {});
    dialog.showModal();

    const onScan = (value) => {
      targetInput.value = value;
      playBeep();
      dialog.close();
    };

    if (backend === 'native') {
      stopScan = startNative(video, onScan);
    } else {
      startZXing(video, onScan).then(stop => {
        if (cancelled) stop();
        else stopScan = stop;
      });
    }
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const beepToggle = document.getElementById('scanner-beep-toggle');
    if (beepToggle) {
      beepToggle.checked = localStorage.getItem(BEEP_KEY) !== 'false';
      beepToggle.addEventListener('change', () => {
        localStorage.setItem(BEEP_KEY, beepToggle.checked ? 'true' : 'false');
      });
    }

    const backend = await detectBackend();

    document.querySelectorAll('[data-scan-target]').forEach(btn => {
      const target = document.getElementById(btn.dataset.scanTarget);
      if (!target) return;
      if (!backend) {
        btn.disabled = true;
        btn.title = 'Camera scanning unavailable — type or use a USB scanner';
        return;
      }
      btn.addEventListener('click', () => openScanner(target, backend));
    });
  });
})();
