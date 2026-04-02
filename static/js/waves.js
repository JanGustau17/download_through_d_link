/* ── Sound Wave Animations ─────────────────────────────────────────────────── */

// ── 1. Decorative Hero Wave (landing page background) ────────────────────────
class HeroWave {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.running = true;
    this.time = 0;
    this.resize();
    window.addEventListener('resize', () => this.resize());
    this.animate();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.ctx.scale(dpr, dpr);
    this.w = rect.width;
    this.h = rect.height;
  }

  drawWave(yBase, amplitude, frequency, speed, color, lineWidth) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;

    for (let x = 0; x <= this.w; x += 2) {
      const y = yBase +
        Math.sin(x * frequency + this.time * speed) * amplitude +
        Math.sin(x * frequency * 0.5 + this.time * speed * 1.3) * (amplitude * 0.4);
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  animate() {
    if (!this.running) return;
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);

    const mid = this.h * 0.5;

    // Layer 1: subtle background wave
    this.drawWave(mid, 12, 0.008, 0.6, 'rgba(99, 179, 237, 0.08)', 2);
    // Layer 2: main accent wave
    this.drawWave(mid, 18, 0.012, 0.8, 'rgba(99, 179, 237, 0.15)', 2.5);
    // Layer 3: purple accent
    this.drawWave(mid, 10, 0.015, 1.0, 'rgba(183, 148, 244, 0.12)', 1.5);
    // Layer 4: teal highlight
    this.drawWave(mid, 8, 0.02, 1.2, 'rgba(56, 178, 172, 0.08)', 1);

    this.time += 0.02;
    requestAnimationFrame(() => this.animate());
  }

  destroy() {
    this.running = false;
  }
}

// ── 2. Audio Visualizer (bars + wave when audio plays) ───────────────────────
class AudioVisualizer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.running = false;
    this.audioCtx = null;
    this.analyser = null;
    this.dataArray = null;
    this.source = null;
    this.resize();
    window.addEventListener('resize', () => this.resize());
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.ctx.scale(dpr, dpr);
    this.w = rect.width;
    this.h = rect.height;
  }

  connectAudio(audioElement) {
    if (!this.audioCtx) {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (this.source) {
      try { this.source.disconnect(); } catch(e) {}
    }

    this.source = this.audioCtx.createMediaElementSource(audioElement);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 128;
    this.analyser.smoothingTimeConstant = 0.8;

    this.source.connect(this.analyser);
    this.analyser.connect(this.audioCtx.destination);

    this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.canvas.style.display = 'block';
    this.draw();
  }

  stop() {
    this.running = false;
    this.canvas.style.display = 'none';
  }

  draw() {
    if (!this.running) return;
    requestAnimationFrame(() => this.draw());

    if (!this.analyser) return;
    this.analyser.getByteFrequencyData(this.dataArray);

    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);

    const bars = this.dataArray.length;
    const barWidth = this.w / bars;
    const centerY = this.h / 2;

    for (let i = 0; i < bars; i++) {
      const val = this.dataArray[i] / 255;
      const barH = val * centerY * 0.9;

      // Color gradient: blue -> purple -> teal based on frequency
      const hue = 200 + (i / bars) * 80; // 200 (blue) to 280 (purple)
      const alpha = 0.4 + val * 0.5;

      ctx.fillStyle = `hsla(${hue}, 70%, 65%, ${alpha})`;

      // Mirrored bars (up and down from center)
      const x = i * barWidth;
      const bw = barWidth * 0.7;

      // Round-capped bars
      const radius = bw / 2;
      if (barH > 1) {
        // Top bar
        ctx.beginPath();
        ctx.roundRect(x + barWidth * 0.15, centerY - barH, bw, barH, radius);
        ctx.fill();
        // Bottom bar (mirrored)
        ctx.beginPath();
        ctx.roundRect(x + barWidth * 0.15, centerY, bw, barH, radius);
        ctx.fill();
      } else {
        // Idle: thin line
        ctx.fillStyle = `hsla(${hue}, 70%, 65%, 0.15)`;
        ctx.fillRect(x + barWidth * 0.15, centerY - 1, bw, 2);
      }
    }
  }

  // Idle animation (no audio connected) — gentle faux wave
  drawIdle() {
    if (!this.running) return;
    requestAnimationFrame(() => this.drawIdle());

    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);

    const bars = 64;
    const barWidth = this.w / bars;
    const centerY = this.h / 2;
    const time = Date.now() / 1000;

    for (let i = 0; i < bars; i++) {
      const val = (Math.sin(i * 0.3 + time * 2) * 0.3 + 0.3) *
                  (Math.sin(i * 0.1 + time * 0.8) * 0.2 + 0.5);
      const barH = val * centerY * 0.5;
      const hue = 200 + (i / bars) * 80;

      ctx.fillStyle = `hsla(${hue}, 70%, 65%, ${0.15 + val * 0.2})`;

      const x = i * barWidth;
      const bw = barWidth * 0.7;
      const radius = bw / 2;

      ctx.beginPath();
      ctx.roundRect(x + barWidth * 0.15, centerY - barH, bw, barH, radius);
      ctx.fill();
      ctx.beginPath();
      ctx.roundRect(x + barWidth * 0.15, centerY, bw, barH, radius);
      ctx.fill();
    }
  }

  startIdle() {
    if (this.running) return;
    this.running = true;
    this.canvas.style.display = 'block';
    this.drawIdle();
  }
}

// Export for use in app.js
window.HeroWave = HeroWave;
window.AudioVisualizer = AudioVisualizer;
