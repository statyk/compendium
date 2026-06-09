// Barcode scanning: native BarcodeDetector (Chromium) with ZXing fallback (Safari/Firefox).
// USB keyboard-wedge scanners work as keyboard input automatically — no JS path required.

(function () {
  'use strict';

  const BEEP_KEY = 'compendium-beep-enabled';

  // Backend resolved once by detectBackend() on DOMContentLoaded; read by
  // startRemoteScan. Module-scoped so it does not depend on the published
  // global staying intact.
  let detectedBackend = null;

  // How many consecutive no-detection ticks must pass before the same code
  // is accepted again (i.e. the barcode left the frame and came back).
  const BURST_MISS_THRESHOLD = 8;

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

  // Public API — consumed downstream (LitCat); changes go in CHANGELOG
  function runContinuous(video, backend, { onCode, onMiss } = {}) {
    let active = true;
    let lastAccepted = null;
    let missCount = 0;
    let zxingControls = null;

    function handleDetected(rawValue) {
      if (!active) return;
      if (rawValue === lastAccepted && missCount < BURST_MISS_THRESHOLD) {
        // Same code still in frame — suppress burst repeat.
        return;
      }
      lastAccepted = rawValue;
      missCount = 0;
      onCode(rawValue);
    }

    function handleMiss() {
      if (!active) return;
      missCount++;
      if (onMiss) onMiss();
    }

    function stop() {
      active = false;
      if (zxingControls) { zxingControls.stop(); zxingControls = null; }
    }

    if (backend === 'native') {
      const detector = new BarcodeDetector({
        formats: ['ean_13', 'ean_8', 'code_128', 'code_39', 'upc_a', 'upc_e', 'qr_code'],
      });

      function tick() {
        if (!active) return;
        detector.detect(video)
          .then(results => {
            if (!active) return;
            if (results.length > 0) {
              handleDetected(results[0].rawValue);
            } else {
              handleMiss();
            }
            requestAnimationFrame(tick);
          })
          .catch(() => active && requestAnimationFrame(tick));
      }
      requestAnimationFrame(tick);
    } else {
      // ZXing path: decodeFromStream fires continuously via its own internal loop.
      const reader = new ZXingBrowser.BrowserMultiFormatReader();
      reader.decodeFromStream(video.srcObject, video, (result) => {
        if (!active) return;
        if (result) {
          handleDetected(result.getText());
        } else {
          handleMiss();
        }
      }).then(controls => {
        if (!active) { controls.stop(); return; }
        zxingControls = controls;
      }).catch(() => {});
    }

    return stop;
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

    stopScan = runContinuous(video, backend, {
      onCode: (value) => {
        // One-shot: stop immediately after the first accepted code.
        if (stopScan) { stopScan(); stopScan = null; }
        targetInput.value = value;
        playBeep();
        dialog.close();
      },
    });

    if (cancelled) {
      // Cleaned up before runContinuous returned (e.g. dialog closed synchronously).
      if (stopScan) { stopScan(); stopScan = null; }
    }
  }

  // Public API — consumed downstream (LitCat); changes go in CHANGELOG
  function startRemoteScan(video, { post } = {}) {
    if (!detectedBackend) {
      // Called before detectBackend() resolved; fall back to a no-op stop fn.
      return () => {};
    }
    return runContinuous(video, detectedBackend, {
      onCode: (code) => {
        if (post) post(code);
      },
    });
  }

  // Expose the public API on a stable global namespace so phone-page inline
  // scripts and downstream consumers (LitCat) can reach these functions.
  // Internals (playBeep, stopStream, openScanner) are NOT exposed.
  window.CompendiumScanner = {
    detectBackend,
    runContinuous,
    startRemoteScan,
  };

  document.addEventListener('DOMContentLoaded', async () => {
    const beepToggle = document.getElementById('scanner-beep-toggle');
    if (beepToggle) {
      beepToggle.checked = localStorage.getItem(BEEP_KEY) !== 'false';
      beepToggle.addEventListener('change', () => {
        localStorage.setItem(BEEP_KEY, beepToggle.checked ? 'true' : 'false');
      });
    }

    const backend = await detectBackend();
    detectedBackend = backend;

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
