// Audio management for new event notifications

class AudioManager {
  private ctx: AudioContext | null = null;
  private enabled = false;
  private lastPlayedIds = new Set<string>();
  private maxTrackedIds = 200;

  get isEnabled(): boolean {
    return this.enabled;
  }

  /** ユーザー操作で AudioContext をアンロック */
  unlock(): boolean {
    try {
      if (!this.ctx) {
        this.ctx = new AudioContext();
      }
      if (this.ctx.state === "suspended") {
        this.ctx.resume();
      }
      this.enabled = true;
      try {
        localStorage.setItem("tdnet_audio_enabled", "true");
      } catch { /* ignore */ }
      return true;
    } catch {
      return false;
    }
  }

  disable(): void {
    this.enabled = false;
    try {
      localStorage.setItem("tdnet_audio_enabled", "false");
    } catch { /* ignore */ }
  }

  toggle(): boolean {
    if (this.enabled) {
      this.disable();
      return false;
    } else {
      return this.unlock();
    }
  }

  /** 初回ロード時に localStorage から復元 */
  restoreFromStorage(): void {
    try {
      const stored = localStorage.getItem("tdnet_audio_enabled");
      if (stored === "true") {
        this.unlock();
      }
    } catch { /* ignore */ }
  }

  /** 新着イベントの音を再生 (重複防止付き) */
  async playNotification(eventId: string): Promise<void> {
    if (!this.enabled || !this.ctx) return;
    if (this.lastPlayedIds.has(eventId)) return;

    // Track played IDs (cap at maxTrackedIds)
    this.lastPlayedIds.add(eventId);
    if (this.lastPlayedIds.size > this.maxTrackedIds) {
      const first = this.lastPlayedIds.values().next().value;
      if (first) this.lastPlayedIds.delete(first);
    }

    try {
      await this.ctx.resume();
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.connect(gain);
      gain.connect(this.ctx.destination);

      // ピンポン音: 2回の短いトーン
      const now = this.ctx.currentTime;
      osc.frequency.setValueAtTime(880, now);      // A5
      osc.frequency.setValueAtTime(1047, now + 0.1); // C6
      gain.gain.setValueAtTime(0.3, now);
      gain.gain.linearRampToValueAtTime(0.15, now + 0.08);
      gain.gain.setValueAtTime(0.25, now + 0.1);
      gain.gain.linearRampToValueAtTime(0, now + 0.2);

      osc.start(now);
      osc.stop(now + 0.25);
    } catch {
      // 音再生失敗は握りつぶす
    }
  }
}

// Singleton
export const audioManager = new AudioManager();
