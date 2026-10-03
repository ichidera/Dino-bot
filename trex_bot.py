#!/usr/bin/env python3
# T-Rex Runner Bot - v6.2 (jump-arc planner + 3 modes)
# Setup:  pip install playwright && playwright install chromium     (only mode 1 needs the bundled Chromium)
# Run:    python trex_bot_v6.py                 (menu)
#         python trex_bot_v6.py --mode 2        (skip the menu)
#         python trex_bot_v6.py --mode 3 --offline-in regular
#
# MODES
#   1  Chromium         Playwright's own Chromium, plays trex-runner.com.
#   2  Regular session  Your installed Chrome/Edge/Brave. Attaches over the DevTools port, or launches
#                       the browser with that port open. NOTE: since Chrome 136 the debugging port is
#                       ignored on your DEFAULT profile, so this uses a dedicated profile folder
#                       (~/.trex_bot_profile). It keeps logins/extensions between runs.
#   3  Offline          Takes ONE tab off the internet (per-tab network emulation, other tabs are
#                       untouched) so Chrome shows its built-in dino page, and plays that.
#
# Env overrides: TREX_CDP_PORT (default 9222), TREX_PROFILE (profile dir), TREX_BROWSER (path to browser exe)
#
# The bot itself (BOT_JS) is the v6.1 jump-arc planner: it simulates the game's own jump physics and
# hitboxes every frame and takes off in the middle of the safe window. Mid/low birds are jumped,
# high birds are walked under. Set DUCK_MID = true in the JS to duck mid birds instead.

import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

STOP = threading.Event()   # set by Ctrl+C; sync Playwright can swallow KeyboardInterrupt, so we use a flag

GAME_URL    = "https://trex-runner.com"
OFFLINE_URL = "http://example.com/"     # never actually loads: the tab is offline when we visit it
CDP_PORT    = int(os.environ.get("TREX_CDP_PORT", "9222"))
CDP_URL     = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.environ.get("TREX_PROFILE", os.path.join(os.path.expanduser("~"), ".trex_bot_profile"))

# one JS expression for "the running game", valid on old (instance_) and current (getInstance) builds
GAME = ("(typeof Runner !== 'undefined' ? (Runner.instance_ || "
        "(typeof Runner.getInstance === 'function' ? Runner.getInstance() : null)) : null)")

BOT_JS = """
(function () {
  if (window.__trexBot) { console.log('[BOT] already injected'); return; }
  window.__trexBot = true;

  let stopped = false;
  const log = m => {   // buffered in the page so Python can poll it (works even where console events don't)
    try { const b = (window.__botLogs = window.__botLogs || []); b.push(m); if (b.length > 800) b.shift(); } catch (e) {}
    console.log(m);
  };
  window.__trexBotStop = function () { stopped = true; window.__trexBot = false; };

  const FRAME_MS        = 1000 / 60;
  const PTERO_DUCK_DIST = 250;   // start ducking a mid-height ptero this close
  const LOOK_AHEAD      = 600;   // only plan a jump when the target is this close (px)
  const MAX_K           = 70;    // takeoff delays (in frames) the planner tries
  const SIM_FRAMES      = 220;
  const MARGIN          = 2;     // px hitbox inflation for safety
  const DESPERATE_DIST  = 140;
  const DUCK_MID        = false;  // v6 died 3/3 ducking under y=75 birds on trex-runner.com, so jump them instead
  const ACC             = 0.001;  // currentSpeed gain per update
  const MAX_SPEED       = 13;
  const MAX_DEPTH       = 3;      // how many jumps in a row the planner may chain
  const RE_J            = 24;     // after landing, re-jump delays tried (frames)

  // Chrome dino hitboxes
  const RUN_BOXES  = [
    {x:22,y:0,width:17,height:16},{x:1,y:18,width:30,height:9},{x:10,y:35,width:14,height:8},
    {x:1,y:24,width:29,height:5},{x:5,y:30,width:21,height:4},{x:9,y:34,width:15,height:4}];
  const DUCK_BOXES = [{x:1,y:18,width:55,height:25}];

  let jumpCount = 0, tickCount = 0, ducking = false, jumpedObs = null, dropped = false;
  let pauseUntil = 0, lastSeen = 'none', lastTs = 0, dtAvg = FRAME_MS, announced = false;

  // old builds (trex-runner.com): Runner.instance_ ; current Chromium offline page: Runner.getInstance()
  const getGame = () => {
    if (typeof Runner === 'undefined') return null;
    return Runner.instance_ || (typeof Runner.getInstance === 'function' ? Runner.getInstance() : null) || null;
  };
  const scoreOf = g => Math.ceil(g.distanceRan * 0.025);
  const isPtero = o => /ptero/i.test(o.typeConfig.type);
  const widthOf = o => o.width || (o.typeConfig.width * (o.size || 1));

  // ptero heights: 100 low (jump), 75 mid (duck), 50 high (run under)
  function classify(o) {
    if (!isPtero(o)) return 'JUMP';
    if (o.yPos >= 90) return 'JUMP';
    if (o.yPos >= 60) return DUCK_MID ? 'DUCK' : 'JUMP';
    return 'IGNORE';
  }
  const isDucked = dino => (dino.ducking !== undefined) ? !!dino.ducking : ducking;
  function unduck(dino) { if (isDucked(dino) || ducking) { dino.setDuck(false); ducking = false; } }

  function physics(dino) {
    const c = dino.config || {};
    const CBX = dino.constructor && dino.constructor.collisionBoxes;
    const ground = (dino.groundYPos !== undefined) ? dino.groundYPos : 93;
    const pick = (a, b, d) => (a !== undefined ? a : (b !== undefined ? b : d));
    return {
      W: pick(c.WIDTH, undefined, 44), H: pick(c.HEIGHT, undefined, 47),
      g: pick(c.GRAVITY, undefined, 0.6),
      v0: pick(c.INIITAL_JUMP_VELOCITY, c.INITIAL_JUMP_VELOCITY, -10),   // sic, typo is in Chrome's source
      maxH: pick(c.MAX_JUMP_HEIGHT, undefined, 30),
      dropV: pick(c.DROP_VELOCITY, undefined, -5),
      sdc: pick(c.SPEED_DROP_COEFFICIENT, undefined, 3),
      ground: ground,
      run: (CBX && CBX.RUNNING && CBX.RUNNING.length) ? CBX.RUNNING : RUN_BOXES,
      duck: (CBX && CBX.DUCKING && CBX.DUCKING.length) ? CBX.DUCKING : DUCK_BOXES,
      minJump: (dino.minJumpHeight !== undefined) ? dino.minJumpHeight : ground - pick(c.MIN_JUMP_HEIGHT, undefined, 30),
    };
  }

  function prep(o) {
    if (o.__w === undefined) {
      o.__w = widthOf(o);
      o.__boxes = (o.collisionBoxes && o.collisionBoxes.length) ? o.collisionBoxes
                : [{x:0, y:0, width:o.__w, height:o.typeConfig.height}];
    }
  }

  // same two-stage test Chrome's checkForCollision does
  function collide(P, boxes, dx, dy, o, ox) {
    const tx = dx + 1, ty = dy + 1, tw = P.W - 2, th = P.H - 2;
    const bx = ox + 1, by = o.yPos + 1, bw = o.__w - 2, bh = o.typeConfig.height - 2;
    if (!(tx < bx + bw && tx + tw > bx && ty < by + bh && ty + th > by)) return false;
    for (let i = 0; i < boxes.length; i++) {
      const t = boxes[i], ax = t.x + tx, ay = t.y + ty;
      for (let j = 0; j < o.__boxes.length; j++) {
        const b = o.__boxes[j];
        const cx = b.x + bx - MARGIN, cy = b.y + by - MARGIN, cw = b.width + 2 * MARGIN, ch = b.height + 2 * MARGIN;
        if (ax < cx + cw && ax + t.width > cx && ay < cy + ch && ay + t.height > cy) return true;
      }
    }
    return false;
  }

  let fracMode = false;   // true if this game moves obstacles by fractional px (no floor)
  const BIASES = [-0.4, 0, 0.4];   // plan must survive obstacles a bit slower AND a bit faster than predicted
  function stepPx(o, sp, dt, bias) {
    const raw = (sp + (o.speedOffset || 0)) * 60 / 1000 * dt + bias;
    return fracMode ? raw : Math.floor(raw);
  }
  function firstJumpIdx(S, xs) {
    for (let i = 0; i < S.hazards.length; i++) {
      if (xs[i] + S.hazards[i].__w < S.dx - 2) continue;
      if (classify(S.hazards[i]) === 'JUMP') return i;
    }
    return -1;
  }

  function surviveAll(S, st, depth) {
    for (let b = 0; b < S.biases.length; b++) {
      S.bias = S.biases[b];
      if (!survive(S, st, depth)) return false;
    }
    return true;
  }

  // Roll the world forward from state st. true = survives.
  // Mirrors what the bot itself will do: take off at st.jumpAt, speed-drop once the
  // target is behind us (if that's safe), and re-jump after landing if more is coming.
  function survive(S, st, depth) {
    const P = S.P, dx = S.dx, hz = S.hazards, fe = S.dt / FRAME_MS;
    let y = st.y, v = st.v, jumping = st.jumping, reached = st.reached, drop = st.drop, tIdx = st.tIdx;
    const n = hz.length, xs = st.xs.slice();
    for (let f = st.f; f < SIM_FRAMES; f++) {
      const sp = Math.min(MAX_SPEED, S.speed + ACC * f);
      if (f === st.jumpAt && !jumping) { jumping = true; v = P.v0 - sp / 10; reached = false; drop = false; }
      const was = jumping;
      if (jumping) {
        y += Math.round(v * (drop ? P.sdc : 1) * fe);
        v += P.g * fe;
        if (y < P.minJump || drop) reached = true;
        if ((y < P.maxH || drop) && reached && v < P.dropV) v = P.dropV;
        if (y > P.ground) { y = P.ground; jumping = false; v = 0; }
      }
      const landed = was && !jumping;
      let allPast = true;
      for (let i = 0; i < n; i++) {
        xs[i] -= stepPx(hz[i], sp, S.dt, S.bias);
        const o = hz[i];
        if (xs[i] + o.__w >= dx - 2) allPast = false;
        if (jumping || y < P.ground) {
          if (collide(P, P.run, dx, y, o, xs[i])) return false;
        } else if (collide(P, P.run, dx, y, o, xs[i]) &&
                   (!DUCK_MID || !isPtero(o) || collide(P, P.duck, dx, y, o, xs[i]))) return false;
      }
      if (allPast && !jumping) return true;

      if (jumping && !drop && tIdx >= 0 && xs[tIdx] + hz[tIdx].__w < dx - 2) {
        const r = survive(S, {f: f + 1, y: y, v: 1, jumping: true, reached: true, drop: true,
                              tIdx: -1, jumpAt: -1, xs: xs}, depth);
        if (r) return true;
        tIdx = -1;                       // dropping isn't safe here, keep floating
      }
      if (landed && depth < MAX_DEPTH) {
        const tgt = firstJumpIdx(S, xs);
        if (tgt < 0) continue;
        if (survive(S, {f: f + 1, y: y, v: 0, jumping: false, reached: false, drop: false,
                        tIdx: -1, jumpAt: -1, xs: xs}, MAX_DEPTH)) return true;      // just keep running
        for (let j = 0; j <= RE_J; j += 2) {
          if (survive(S, {f: f + 1, y: y, v: 0, jumping: false, reached: false, drop: false,
                          tIdx: tgt, jumpAt: f + 1 + j, xs: xs}, depth + 1)) return true;
        }
        return false;
      }
    }
    return true;
  }

  function tick(ts) {
    if (stopped) return;
    requestAnimationFrame(tick);
    const game = getGame();
    if (!game) return;
    const now = performance.now();
    if (now < pauseUntil) { lastTs = now; return; }
    if (lastTs) { const d = Math.min(40, Math.max(4, now - lastTs)); dtAvg = dtAvg * 0.9 + d * 0.1; }
    lastTs = now;

    if (game.crashed) {
      const dd = game.tRex;
      log('[DEATH] score=' + scoreOf(game) + ' speed=' + game.currentSpeed.toFixed(2) + ' | ' + lastSeen +
                  ' | dino x=' + dd.xPos + ' y=' + dd.yPos + ' status=' + dd.status + ' ducking=' + dd.ducking + ' jumping=' + dd.jumping);
      jumpCount = 0; ducking = false; jumpedObs = null; dropped = false; lastSeen = 'none';
      game.restart();
      pauseUntil = performance.now() + 800;
      return;
    }
    if (!game.activated) return;

    tickCount++;
    const speed = game.currentSpeed, dino = game.tRex, dt = dtAvg;
    const all = (game.horizon && game.horizon.obstacles) || [];
    const P = physics(dino);

    if (!announced) {
      announced = true;
      log('[BOT] physics dino.xPos=' + dino.xPos + ' ground=' + P.ground + ' minJump=' + P.minJump +
                  ' g=' + P.g + ' v0=' + P.v0 + ' maxH=' + P.maxH + ' dropV=' + P.dropV);
      const bx = a => JSON.stringify(a.map(b => [b.x, b.y, b.width, b.height]));
      log('[BOT] dino boxes run=' + bx(P.run) + ' duck=' + bx(P.duck) + ' W=' + P.W + ' H=' + P.H +
                  ' H_DUCK=' + (dino.config && dino.config.HEIGHT_DUCK));
    }
    if (tickCount % 180 === 0)
      log('[BOT] speed=' + speed.toFixed(2) + ' jumps=' + jumpCount + ' score=' + scoreOf(game) +
                  ' fps~' + (1000 / dt).toFixed(0));

    // everything not yet fully behind the dino
    const ahead = [];
    for (let i = 0; i < all.length && ahead.length < 4; i++) {
      const o = all[i];
      prep(o);
      if (o.xPos % 1 !== 0) fracMode = true;
      if (o.xPos + o.__w < dino.xPos - 2) continue;
      ahead.push(o);
    }

    let jumpT = null, duckT = null;
    for (const o of ahead) {
      const a = classify(o);
      if (a === 'JUMP' && !jumpT) jumpT = o;
      if (a === 'DUCK' && !duckT) duckT = o;
    }
    const first = ahead[0] || null;
    if (first) {
      lastSeen = 'raw=' + first.typeConfig.type + ' size=' + (first.size || 1) + ' y=' + first.yPos +
                 ' dist=' + (first.xPos - dino.xPos).toFixed(0) + ' jumping=' + dino.jumping + ' ducking=' + ducking;
    }

    // log each obstacle once as it comes into range
    for (const o of ahead) {
      const d = o.xPos - dino.xPos;
      if (!o.__logged && d < 400 && d > 0) {
        o.__logged = true;
        if (isPtero(o) && !window.__pteroLogged) {
          window.__pteroLogged = true;
          log('[BOT] ptero y=' + o.yPos + ' boxes=' + JSON.stringify(o.__boxes.map(b => [b.x, b.y, b.width, b.height])) + ' speedOffset=' + o.speedOffset);
        }
        log('[OBS] ' + o.typeConfig.type + ' y=' + o.yPos + ' size=' + (o.size || 1) + ' w=' + o.__w +
                    ' act=' + classify(o) + ' dist=' + d.toFixed(0) + ' speed=' + speed.toFixed(2));
      }
    }

    // ---- pinned: after a speed-drop lands exactly on the ground, current Chromium flips the dino into its
    // ducking animation, whose physics step is ~8x smaller, so it can sit at ground level still flagged
    // "jumping" for ~20 frames and cannot start a new jump. The engine would reset() it eventually; do it now.
    if (dropped && dino.jumping && dino.yPos >= P.ground && typeof dino.reset === 'function') {
      log('[BOT] unpinned dino after speed-drop');
      dino.reset();
      jumpedObs = null; dropped = false;
    }

    // ---- airborne: land early once we're clear, if the simulation says it's safe ----
    if (dino.jumping) {
      if (!dino.speedDrop && !dropped) {
        const clear = jumpedObs && (jumpedObs.xPos + jumpedObs.__w < dino.xPos - 2);
        const pteroSoon = duckT && (duckT.xPos - dino.xPos) < PTERO_DUCK_DIST && (!jumpT || duckT.xPos < jumpT.xPos);
        if (clear || pteroSoon) {
          const S = {P: P, dx: dino.xPos, hazards: ahead, speed: speed, dt: dt, biases: BIASES};
          const st = {f: 0, y: dino.yPos, v: 1, jumping: true, reached: true, drop: true,
                      tIdx: -1, jumpAt: -1, xs: ahead.map(o => o.xPos)};
          if (surviveAll(S, st, 1)) {
            log('[BOT] SPEED-DROP (' + (pteroSoon ? 'ptero' : 'cleared') + ')');
            dino.setSpeedDrop();
            dropped = true;
          }
        }
      }
      return;
    }
    jumpedObs = null;
    dropped = false;

    // ---- on the ground: plan a jump for the next cactus / low ptero ----
    if (jumpT) {
      const dist = jumpT.xPos - dino.xPos;
      if (dist < LOOK_AHEAD) {
        const S = {P: P, dx: dino.xPos, hazards: ahead, speed: speed, dt: dt, biases: BIASES};
        const tIdx = ahead.indexOf(jumpT);
        const xs0 = ahead.map(o => o.xPos);
        const plan = (k, d) => surviveAll(S, {f: 0, y: dino.yPos, v: 0, jumping: false, reached: false, drop: false,
                                           tIdx: tIdx, jumpAt: k, xs: xs0}, d);
        // Prefer a plan with ONE jump; only chain jumps if no single-jump plan exists at all.
        // Plan pessimistically first; if that finds nothing at all, retry with exact physics.
        const compute = biases => {
          S.biases = biases;
          let depth = MAX_DEPTH, anyLater = false;
          for (let k = 0; k <= MAX_K && !anyLater; k++) if (plan(k, MAX_DEPTH)) anyLater = true;
          if (!anyLater) { depth = 0; for (let k = 0; k <= MAX_K && !anyLater; k++) if (plan(k, 0)) anyLater = true; }
          let r = 0;
          while (r <= MAX_K && plan(r, depth)) r++;
          return {depth: depth, anyLater: anyLater, R: r};
        };
        let res = compute(BIASES);
        let relaxed = false;
        if (!res.anyLater) { res = compute([0]); relaxed = true; }
        const R = res.R;
        if (R > 0) {
          if (jumpT.__W === undefined || R > jumpT.__W) jumpT.__W = R;
          // take off in the middle of the safe window, so timing error has room on both sides
          if (R <= Math.ceil(jumpT.__W / 2)) {
            log('[BOT] JUMP ' + jumpT.typeConfig.type + ' size=' + (jumpT.size || 1) + ' dist=' + dist.toFixed(0) +
                        ' window=' + jumpT.__W + ' left=' + R + ' speed=' + speed.toFixed(2) + (relaxed ? ' [exact-physics plan]' : ''));
            unduck(dino);
            dino.startJump(speed);
            jumpedObs = jumpT;
            jumpCount++;
            return;
          }
        } else if (dist < DESPERATE_DIST) {
          if (!res.anyLater) {
            log('[BOT] JUMP (no safe window found) dist=' + dist.toFixed(0));
            unduck(dino); dino.startJump(speed); jumpedObs = jumpT; jumpCount++;
            return;
          }
        }
      }
    }

    // ---- mid-height ptero: duck, but only if it's the nearer threat ----
    if (duckT && (duckT.xPos - dino.xPos) < PTERO_DUCK_DIST && (!jumpT || duckT.xPos < jumpT.xPos)) {
      if (!isDucked(dino)) {
        log('[BOT] DUCK ptero y=' + duckT.yPos + ' dist=' + (duckT.xPos - dino.xPos).toFixed(0));
        dino.setDuck(true); ducking = true;
      }
      return;
    }
    unduck(dino);
  }

  function waitForGame() {
    if (stopped) return;
    if (getGame()) { log('[BOT] ready'); requestAnimationFrame(tick); }
    else setTimeout(waitForGame, 300);
  }
  log('[BOT] injected');
  waitForGame();
})();

"""


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

class Tab:
    """A page plus a way to run JS in it even where page.evaluate can't (e.g. browser error pages)."""

    def __init__(self, page):
        self.page = page
        self._cdp = None

    def cdp(self):
        if self._cdp is None:
            self._cdp = self.page.context.new_cdp_session(self.page)
        return self._cdp

    def js(self, expr):
        try:
            return self.page.evaluate(expr)
        except Exception:
            r = self.cdp().send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            if "exceptionDetails" in r:
                raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
            return r.get("result", {}).get("value")


def ask(prompt, default=None):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def menu():
    print()
    print("  T-Rex bot - pick a mode")
    print("  1) Chromium         Playwright's own Chromium, plays trex-runner.com")
    print("  2) Regular session  your installed Chrome/Edge, plays trex-runner.com in a tab")
    print("  3) Offline          takes ONE tab offline and plays Chrome's built-in dino")
    print()
    while True:
        v = ask("  Select [1-3]: ")
        if v in ("1", "2", "3"):
            return v
        print("  Please type 1, 2 or 3.")


def cdp_alive():
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def find_browser():
    env = os.environ.get("TREX_BROWSER")
    if env and os.path.exists(env):
        return env
    system = platform.system()
    cands = []
    if system == "Windows":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        roots = [r for r in roots if r]
        cands += [os.path.join(r, "Google", "Chrome", "Application", "chrome.exe") for r in roots]
        cands += [os.path.join(r, "Microsoft", "Edge", "Application", "msedge.exe") for r in roots]
        cands += [os.path.join(r, "BraveSoftware", "Brave-Browser", "Application", "brave.exe") for r in roots]
    elif system == "Darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def launch_debug_browser(exe):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [exe, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run", "--no-default-browser-check", "about:blank"]
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)


def attach_regular(p):
    """Return the default browser context of a real Chrome/Edge that has the debugging port open."""
    if cdp_alive():
        print(f"[*] found a browser already listening on {CDP_URL}")
    else:
        exe = find_browser()
        if not exe:
            raise SystemExit("[!] couldn't find Chrome/Edge/Brave. Set TREX_BROWSER to its path, or start it yourself with\n"
                             f'    --remote-debugging-port={CDP_PORT} --user-data-dir="{PROFILE_DIR}"')
        print(f"[*] launching {exe}")
        print(f"    profile: {PROFILE_DIR}  (port {CDP_PORT})")
        launch_debug_browser(exe)
        for _ in range(60):
            if cdp_alive():
                break
            time.sleep(0.5)
        else:
            raise SystemExit("[!] the browser didn't open its debugging port in 30s. Close any window that uses this "
                             "profile and try again, or set TREX_CDP_PORT to another port.")
    browser = p.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, context


def wait_for_game(tab, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if tab.js(f"!!{GAME}"):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_game(tab):
    """Press SPACE in the tab so the run begins (the bot restarts itself after every death)."""
    try:
        tab.page.bring_to_front()
        for _ in range(3):
            if tab.js(f"!!({GAME} && {GAME}.activated)"):
                return
            tab.page.keyboard.press("Space")
            time.sleep(0.8)
        if tab.js(f"!!({GAME} && {GAME}.activated)"):
            return
    except Exception:
        pass
    print("[!] couldn't confirm the game started - press SPACE in the game tab yourself.")


def play(tab):
    print("[*] injecting bot...")
    tab.js(BOT_JS)
    start_game(tab)
    print("[*] bot running. Ctrl+C to stop.\n")
    old = signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        while not STOP.is_set():
            for line in tab.js("window.__botLogs ? window.__botLogs.splice(0) : []") or []:
                print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            tab.page.wait_for_timeout(250)    # NOT time.sleep: keeps Playwright's event loop alive
        print("\n[*] stopping")
    except KeyboardInterrupt:
        print("\n[*] stopping")
    except Exception as e:
        print(f"\n[*] tab or browser closed ({type(e).__name__})")
    finally:
        signal.signal(signal.SIGINT, old)
        try:
            tab.js("window.__trexBotStop && window.__trexBotStop()")
        except Exception:
            pass


# ----------------------------------------------------------------------------------------------
# modes
# ----------------------------------------------------------------------------------------------

def open_game_tab(context):
    page = context.new_page()
    print(f"[*] opening {GAME_URL} ...")
    page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
    tab = Tab(page)
    print("[*] waiting for Runner...")
    if not wait_for_game(tab):
        raise SystemExit("[!] Runner never appeared on that page")
    return tab


def run_chromium(p):
    browser = p.chromium.launch(headless=False)
    try:
        tab = open_game_tab(browser.new_context())
        play(tab)
    finally:
        try:
            browser.close()
        except Exception:
            pass


def run_regular(p):
    browser, context = attach_regular(p)
    # offer an already-open tab that has the game, otherwise open a fresh one
    found = []
    for pg in context.pages:
        t = Tab(pg)
        try:
            if t.js(f"!!{GAME}"):
                found.append(t)
        except Exception:
            pass
    tab = None
    if found:
        print("[*] tabs that already have the game:")
        print("    0) open a new tab")
        for i, t in enumerate(found, 1):
            try:
                title = t.page.title()
            except Exception:
                title = "?"
            print(f"    {i}) {title} - {t.page.url}")
        v = ask("    Use which? [0]: ", "0")
        if v.isdigit() and 1 <= int(v) <= len(found):
            tab = found[int(v) - 1]
    if tab is None:
        tab = open_game_tab(context)
    play(tab)
    print("[*] your browser and tab are left open.")


def run_offline(p, where=None):
    if where is None:
        v = ask("  Run the offline tab in:  1) Chromium   2) your regular session   [1]: ", "1")
        where = "regular" if v == "2" else "chromium"
    owned = None
    if where == "regular":
        browser, context = attach_regular(p)
    else:
        owned = browser = p.chromium.launch(headless=False)
        context = browser.new_context()
    try:
        page = context.new_page()
        tab = Tab(page)
        cdp = tab.cdp()
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions",
                 {"offline": True, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
        print("[*] this tab is now OFFLINE (your other tabs are untouched)")
        try:
            page.goto(OFFLINE_URL, wait_until="commit", timeout=15000)
        except Exception as e:
            print(f"[*] offline error page is up ({type(e).__name__}) - that is expected")
        print("[*] waiting for the built-in dino game...")
        if not wait_for_game(tab, 12):
            raise SystemExit("[!] no Runner on the offline page. If your Chrome has the dino game disabled "
                             "(policy / flags), use mode 1 or 2 instead.")
        play(tab)
        if where == "regular":
            print("[*] the tab stays on the offline page; reload it to get back online.")
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="T-Rex runner bot")
    ap.add_argument("--mode", choices=["1", "2", "3"], help="1=Chromium 2=Regular session 3=Offline")
    ap.add_argument("--offline-in", choices=["chromium", "regular"], help="where mode 3 runs")
    args = ap.parse_args()
    mode = args.mode or menu()
    with sync_playwright() as p:
        if mode == "1":
            run_chromium(p)
        elif mode == "2":
            run_regular(p)
        else:
            run_offline(p, args.offline_in)


if __name__ == "__main__":
    main()