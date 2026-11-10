// Screenshots of every page, desktop (1280x800) and phone (390 wide), light and dark,
// plus the first-run checklist, the guided setup (its first and its folder step), a
// preview result, a night's detail and the paused chip.
// Usage: node tools/ui/screenshots.mjs <out-dir>
// Needs playwright-core (npm install in tools/ui) and KEEPSAKE_CHROMIUM = a Chromium binary.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");
const out = process.argv[2] || "docs/assets/screenshots";
mkdirSync(out, { recursive: true });

let seq = 0;
function fixture(args = []) {
  const py = spawn("python3", ["tools/ui/fixture_server.py", "--workdir", `/tmp/keepsake-shots-${process.pid}-${seq++}`, ...args], {
    env: { ...process.env, PYTHONPATH: "src" }, stdio: ["pipe", "pipe", "inherit"] });
  return new Promise((resolve, reject) => {
    py.stdout.on("data", (d) => { const m = /PORT=(\d+)/.exec(String(d)); if (m) resolve({ py, base: `http://127.0.0.1:${m[1]}` }); });
    py.on("exit", () => reject(new Error("fixture server exited")));
  });
}

const main = await fixture();
const empty = await fixture(["--no-jobs"]);
const paused = await fixture(["--pause", "tomorrow"]);
const wizard = await fixture(["--setup", "--no-anchor"]);          // a brand-new install: only the wizard
const wizardFolders = await fixture(["--setup", "--no-jobs"]);     // iCloud folder confirmed, no folder yet
const browser = await chromium.launch({ executablePath: process.env.KEEPSAKE_CHROMIUM });
const pages = [["folders", "#/folders"], ["add", "#/add"], ["job", "#/job/photos"], ["deleted", "#/deleted/documents"],
  ["activity", "#/activity"], ["settings", "#/settings"], ["settings-alerts", "#/settings/alerts"], ["settings-pause", "#/settings/pause"], ["help", "#/settings/help"]];
const sizes = [["desktop", { width: 1280, height: 800 }], ["phone", { width: 390, height: 844 }]];
try {
  for (const scheme of ["light", "dark"]) {
    for (const [sizeName, viewport] of sizes) {
      const file = (name) => `${out}/keepsake-${name}-${sizeName}-${scheme}.png`;
      const ctx = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      for (const [name, hash] of pages) {
        await page.goto(main.base + "/" + hash);
        await page.waitForSelector("main h1", { timeout: 10000 });
        if (name === "add") {
          await page.waitForSelector(".picker li.prow");
          await page.click('.picker li[data-path="Archive"] button.open');
          await page.click('.picker li[data-path="Archive/2019"] button.open');
          await page.check('.picker li[data-path="Archive/2019/Holidays"] input');
          await page.click('.crumbs button:has-text("Archive")');
          await page.click('.picker li[data-path="Archive/2020"] button.open');
          await page.check('.picker li[data-path="Archive/2020/Holidays"] input');
          await page.waitForSelector(".chosen-list li:nth-child(2)");
        }
        if (name === "folders") await page.waitForSelector(".job .strip");
        await page.waitForTimeout(250);
        await page.screenshot({ path: file(name), fullPage: true });
      }
      // A night's detail (a seeded night that has detail), as the sheet it opens in.
      await page.goto(main.base + "/#/folders");
      await page.waitForSelector(".job .strip");
      await page.locator('.job[data-job="photos"] .strip .nb').nth(9).click();
      await page.waitForSelector("dialog.sheet[open]");
      await page.waitForTimeout(500);
      await page.screenshot({ path: file("night") });
      await page.keyboard.press("Escape");
      // A preview: what accepting the smaller source of Projects would move, nothing written.
      await page.waitForSelector("dialog[open]", { state: "detached" });
      await page.click('button[aria-label="Review projects: the source shrank"]');
      await page.waitForSelector("dialog[open]");
      await page.click('dialog button:has-text("Preview first")');
      await page.waitForSelector("dialog[open] h2:has-text('Preview of projects')");
      await page.waitForTimeout(500);
      await page.screenshot({ path: file("preview") });
      await ctx.close();
      // The guided setup: the first step, and the step where folders are chosen.
      for (const [name, srv, drive] of [["setup-confirm", wizard, async () => {}],
        ["setup-folders", wizardFolders, async (p) => {
          await p.click("#wiz-primary");
          await p.waitForSelector(".picker li.prow");
          await p.check('.picker li.prow[data-path="Photos"] input');
          await p.waitForSelector(".chosen:has-text('1 folder chosen')");
        }]]) {
        const c = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
        const p = await c.newPage();
        await p.goto(srv.base + "/#/setup");
        await p.waitForSelector(".wiz #wiz-h", { timeout: 10000 });
        await drive(p);
        await p.waitForTimeout(250);
        await p.screenshot({ path: file(name), fullPage: true });
        await c.close();
      }
      // A copy that is running: the bar, the phase, the counters and the folder it is in.
      // The status is the real one with one folder's work filled in, so the page is as the
      // server would show it halfway through a large copy.
      {
        const c = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
        const p = await c.newPage();
        await p.route((u) => u.pathname === "/api/status", async (route) => {
          const res = await route.fetch(); const body = await res.json();
          Object.assign(body.jobs.find((j) => j.name === "photos"), { status: "running", operation: "sync", progress_detail: {
            op: "sync", phase: "copying", label: "Copying", reading: false, files: 3120, total_files: 8200, bytes: 14.2 * 1024 ** 3,
            total_bytes: 36.8 * 1024 ** 3, estimated: false, percent: 38.6, path: "2019/05 Holidays", elapsed_s: 1538, idle_s: 1,
            stalled: false, can_cancel: false, cancelling: false } });
          await route.fulfill({ response: res, json: body });
        });
        await p.goto(main.base + "/#/folders");
        await p.waitForSelector('[data-progress="photos"]', { timeout: 10000 });
        await p.waitForTimeout(500);
        await p.screenshot({ path: file("progress"), fullPage: true });
        await c.close();
      }
      // The guided first run, and the top bar while everything is paused.
      for (const [name, srv, wait] of [["first-run", empty, ".checklist"], ["paused", paused, ".job .strip"]]) {
        const c = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: 1 });
        const p = await c.newPage();
        await p.goto(srv.base + "/#/folders");
        await p.waitForSelector(wait, { timeout: 10000 });
        await p.waitForTimeout(250);
        await p.screenshot({ path: file(name), fullPage: true });
        await c.close();
      }
    }
  }
} finally {
  await browser.close();
  for (const s of [main, empty, paused, wizard, wizardFolders]) s.py.stdin.end();
}
console.log("screenshots written to", out);
