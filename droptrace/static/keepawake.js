/* Keeping the display awake, shared by every page.
 *
 * Two mechanisms, because the good one is not always available:
 *
 *  1. The screen Wake Lock API -- the same thing a playing video does. Browsers
 *     only expose it in a secure context, which is HTTPS or localhost. The
 *     dashboard on this machine is localhost, so it works there; a phone opening
 *     http://192.168.x.x gets nothing.
 *  2. A best-effort fallback that mimics video playback: a playing video element
 *     built from a tiny canvas capture stream, in fullscreen. No asset needed.
 *
 * The status line always names which one is in force. A page that claims to have
 * handled this while the screen sleeps through the night is worse than one that
 * tells you to set the timeout yourself.
 *
 * Note what this cannot do: it holds the *display* awake while a page is open and
 * visible. Windows can still sleep the machine, and WSL cannot prevent that --
 * scripts/keep-awake.ps1 on the host is the answer to that one.
 */
(() => {
  "use strict";

  const state = { sentinel: null, stream: null, awake: false, timer: null };

  function makeVideo() {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 2;
    const ctx = canvas.getContext("2d");
    let tick = 0;
    // A frame that changes now and then: a stream with no motion can be treated
    // as an idle tab and throttled.
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      tick += 1;
      ctx.fillStyle = tick % 2 ? "#000" : "#010101";
      ctx.fillRect(0, 0, 2, 2);
    }, 1000);
    state.stream = canvas.captureStream(1);
    const video = document.createElement("video");
    video.id = "awake-video";
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.srcObject = state.stream;
    video.style.cssText =
      "position:fixed;right:6px;bottom:6px;width:2px;height:2px;opacity:.01;pointer-events:none";
    document.body.appendChild(video);
    return video;
  }

  async function takeLock() {
    if (!(navigator.wakeLock && window.isSecureContext)) return false;
    try {
      state.sentinel = await navigator.wakeLock.request("screen");
      state.sentinel.addEventListener("release", () => {
        state.awake = false;
        state.sentinel = null;
        api.report("released — press the button again (the browser drops it whenever the page is hidden)");
      });
      return true;
    } catch (error) {
      return false;
    }
  }

  async function startFallback() {
    try {
      const video = makeVideo();
      await video.play();
      if (document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen().catch(() => {});
      }
      return true;
    } catch (error) {
      return false;
    }
  }

  const api = {
    button: null,
    status: null,

    report(text) {
      if (this.status) this.status.textContent = text;
      if (this.button) {
        this.button.textContent = state.awake ? "Screen kept on ✓" : "Keep screen on";
        this.button.classList.toggle("is-on", state.awake);
      }
    },

    async toggle() {
      if (state.awake) {
        // Let go deliberately: release the lock and stop the fallback stream.
        if (state.sentinel) {
          await state.sentinel.release().catch(() => {});
          state.sentinel = null;
        }
        if (state.stream) {
          state.stream.getTracks().forEach((track) => track.stop());
          state.stream = null;
        }
        const video = document.getElementById("awake-video");
        if (video) video.remove();
        clearInterval(state.timer);
        if (document.fullscreenElement) await document.exitFullscreen().catch(() => {});
        state.awake = false;
        this.report("released — the display may sleep again now");
        return false;
      }
      if (await takeLock()) {
        state.awake = true;
        this.report("kept awake by the Wake Lock API");
        return true;
      }
      if (await startFallback()) {
        state.awake = true;
        this.report("best effort: fullscreen playback over plain HTTP — the device may still sleep");
        return true;
      }
      this.report("could not be kept awake — set the system's screen timeout instead");
      return false;
    },

    attach(button, status) {
      this.button = button;
      this.status = status;
      if (!button) return;
      button.addEventListener("click", () => this.toggle());
      // The lock is released whenever the page is hidden; take it back on return.
      document.addEventListener("visibilitychange", async () => {
        if (document.visibilityState === "visible" && state.awake && !state.sentinel) {
          await takeLock();
        }
      });
      this.report(window.isSecureContext
        ? "not requested — Wake Lock API available here"
        : "not requested — plain HTTP, so only the fallback is possible");
    },
  };

  window.dropTraceKeepAwake = api;
})();
