// UI smoke test in headless Chromium against a throwaway fixture tree.
// Usage: node tests/ui/smoke.mjs      (needs playwright-core and KEEPSAKE_CHROMIUM)
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(new URL("../../tools/ui/", import.meta.url));
const { chromium } = require("playwright-core");

let seq = 0;
function fixture(args = []) {
  const workdir = `/tmp/keepsake-ui-${process.pid}-${seq++}`;
  const py = spawn("python3", ["tools/ui/fixture_server.py", "--workdir", workdir, ...args], {
    env: { ...process.env, PYTHONPATH: "src" }, stdio: ["pipe", "pipe", "inherit"] });
  return new Promise((resolve, reject) => {
    py.stdout.on("data", (d) => { const m = /PORT=(\d+)/.exec(String(d)); if (m) resolve({ py, workdir, base: `http://127.0.0.1:${m[1]}` }); });
    py.on("exit", () => reject(new Error("fixture server exited")));
  });
}

// Every entry under a folder with its type, size, times and a hash of its content:
// two snapshots are equal only if nothing was created, removed, rewritten or touched.
function snapshot(dir) {
  const out = {};
  const walk = (d) => {
    for (const e of readdirSync(d, { withFileTypes: true })) {
      const path = `${d}/${e.name}`, st = lstatSync(path);
      out[path] = [st.mode, st.size, st.mtimeMs, e.isFile() ? createHash("sha1").update(readFileSync(path)).digest("hex") : ""];
      if (e.isDirectory()) walk(path);
    }
  };
  walk(dir);
  return out;
}

// The page's content security policy forbids eval, which page.waitForFunction needs: poll instead.
async function until(page, fn, arg) {
  for (let i = 0; i < 80; i++) {
    if (await page.evaluate(fn, arg)) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error("condition not reached: " + fn);
}

// A row's Preview, Check and Edit are in its "More actions" menu: open it if it is closed, then use the item.
async function rowAction(page, job, label) {
  const item = page.locator(`.job[data-job="${job}"] [role="menuitem"][aria-label="${label}"]`);
  if (!(await item.isVisible())) await page.click(`.job[data-job="${job}"] button.morebtn`);
  await item.click();
}

let passed = 0;
let rescue = null;
async function test(name, fn) {
  try { await fn(); passed++; console.log("ok   " + name); }
  catch (e) {
    console.log("FAIL " + name + "\n     " + (e.stack || e)); process.exitCode = 1;
    if (rescue) await rescue().catch(() => {});   // a failed test must not leave a dialog open over the next one
  }
}

const browser = await chromium.launch({ executablePath: process.env.KEEPSAKE_CHROMIUM });
// KEEPSAKE_UI_LOCALE=en-US runs the whole suite as an English-US browser would see the page (12-hour clocks, other number formats).
if (process.env.KEEPSAKE_UI_LOCALE) { const make = browser.newContext.bind(browser); browser.newContext = (o = {}) => make({ locale: process.env.KEEPSAKE_UI_LOCALE, ...o }); }
const open = fixture();
const { py, base, workdir } = await open;
try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  rescue = () => page.keyboard.press("Escape");
  const external = [], problems = [];
  page.on("request", (r) => { if (!r.url().startsWith(base) && !r.url().startsWith("data:")) external.push(r.url()); });
  // A refused request (400 for a bad folder) is expected and shown to the person as a sentence.
  page.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push(String(e)));

  await test("folders page lists every job with plain-words status, in one table", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    assert.equal(await page.locator("table.folders").count(), 1);
    assert.equal(await page.locator(".job").count(), 4);
    assert.match(await page.locator(".job").nth(2).innerText(), /shrank from 20 to 5 files/);
    assert.match(await page.locator(".job").first().innerText(), /Up to date/);
    assert.match(await page.locator(".job").last().innerText(), /Paused/);
    assert.deepEqual(await page.locator("table.folders thead th").allInnerTexts(),
      ["Folder", "Status", "Last 14 nights", "Last run", "Verified", "Kept", "Actions"]);
    assert.match(await page.locator("main").innerText(), /Recent activity/);
    assert.match(await page.locator(".key").innerText(), /Copied\s+Nothing new\s+Paused or failed\s+No run/);
  });

  await test("the nights strip is 14 real buttons drawn from the server's history, never green for a refused run", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const strip = (name) => page.locator(`.job[data-job="${name}"] .strip`);
    for (const name of ["photos", "documents", "projects", "music"]) {
      assert.equal(await strip(name).locator("button.nb").count(), 14);
      assert.equal(await strip(name).locator("button.nb[tabindex='0']").count(), 1, "one tab stop per strip");
    }
    const box = await strip("photos").locator(".nb").first().boundingBox();
    assert.deepEqual([Math.round(box.width), Math.round(box.height)], [16, 32]);
    const bar = await strip("photos").locator(".nb").first().evaluate((el) => { const c = getComputedStyle(el, "::before"); return [c.width, c.height]; });
    assert.deepEqual(bar, ["9px", "26px"]);
    // Today is the last bar: photos copied 12 files, projects was refused by the shrink guard, music never ran.
    const last = (name) => strip(name).locator(".nb").last();
    assert.equal(await last("photos").getAttribute("class"), "nb");
    assert.match(await last("photos").getAttribute("aria-label"), /^(\w+ \d+|\d+ \w+): copied, 12 added$/);
    assert.equal(await last("photos").getAttribute("tabindex"), "0");
    assert.equal(await last("projects").getAttribute("class"), "nb w");
    assert.match(await last("projects").getAttribute("aria-label"), /: paused or failed$/);
    assert.equal(await strip("music").locator(".nb.e").count(), 14);
    assert.match(await strip("music").locator(".nb").first().getAttribute("aria-label"), /: no run$/);
    assert.match(await strip("projects").getAttribute("aria-label"), /1 paused or failed/);
    assert.match(await strip("music").getAttribute("aria-label"), /14 without a run/);
    assert.equal(await strip("music").getAttribute("role"), "group");
  });

  const row = (path) => `.picker li.prow[data-path="${path}"]`;
  const openRow = (path) => page.click(`${row(path)} button.open`);
  const tick = (path) => page.check(`${row(path)} input`);

  await test("add folder: nothing chosen, the button is off and says why; a tick marks the row and lists it", async () => {
    await page.goto(base + "/#/add");
    await page.waitForSelector(".picker li.prow");
    const names = await page.locator(".picker .pname").allInnerTexts();
    assert.ok(names.some((n) => n.includes("Documents")));
    assert.equal(await page.locator("#f-add").isDisabled(), true);
    assert.match(await page.locator("#f-reason").innerText(), /Choose at least one folder first/);
    assert.match(await page.locator(".chosen").innerText(), /No folders chosen yet/);
    assert.equal(await page.locator(".picker li.prow.on").count(), 0, "nothing is marked before a choice");
    await openRow("Documents");
    await page.waitForSelector(row("Documents/Taxes"));
    await tick("Documents/Taxes");
    assert.match(await page.locator(row("Documents/Taxes")).getAttribute("class"), /\bon\b/);
    assert.equal(await page.locator(`${row("Documents/Taxes")} input`).isChecked(), true);
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen[\s\S]*sources \/ Documents\/Taxes[\s\S]*Keepsake \/ Taxes/);
    assert.equal(await page.locator("#f-add").isDisabled(), false);
  });

  await test("add folder: a refusal is said in plain words and the folder stays chosen", async () => {
    // Documents/Taxes is still chosen from the test before: it overlaps the documents job
    await page.fill("#f-name", "taxes-copy");
    await page.click("#f-add");
    await page.waitForSelector("#toast:not([hidden])");
    assert.match(await page.locator("#toast").innerText(), /overlaps job 'documents'/);
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen/);
  });

  await test("add folder: Open looks inside without choosing, the label and the keyboard choose, x removes", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector(".picker li.prow");
    await openRow("Keys-and-passwords");
    await page.waitForSelector(".picker li.pad");
    assert.match(await page.locator(".picker").innerText(), /No subfolders here/);
    assert.equal(await page.locator(".chosen-list").count(), 0, "opening a folder does not choose it");
    await page.click('.crumbs button:has-text("sources")');
    await page.click(`${row("Scans")} .pname`);                       // the whole row is the label
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen/);
    await page.click('.chosen-list .x[aria-label="Remove sources / Scans"]');
    assert.match(await page.locator(".chosen").innerText(), /No folders chosen yet/);
    assert.equal(await page.locator(`${row("Scans")} input`).isChecked(), false);
    await page.focus(`${row("Scans")} input`);
    await page.keyboard.press("Space");
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen/);
    await page.keyboard.press("Space");
    assert.match(await page.locator(".chosen").innerText(), /No folders chosen yet/);
    assert.equal(await page.locator("#f-add").isDisabled(), true);
  });

  await test("add folder: choices survive moving between folders, several go into ONE destination, a refusal does not hide the rest", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector(".picker li.prow");
    await openRow("Archive");
    await openRow("Archive/2019");
    await tick("Archive/2019/Holidays");
    await page.click('.crumbs button:has-text("Archive")');
    await openRow("Archive/2020");
    await tick("Archive/2020/Holidays");
    await page.click('.crumbs button:has-text("sources")');
    await openRow("Documents");
    await tick("Documents/Taxes");
    const chosen = await page.locator(".chosen").innerText();
    assert.match(chosen, /3 folders chosen/);
    // two sources called Holidays share one destination, each in its own subfolder
    assert.match(chosen, /Archive\/2019\/Holidays[\s\S]*Keepsake \/ Backup \/ Holidays\b/);
    assert.match(chosen, /Archive\/2020\/Holidays[\s\S]*Keepsake \/ Backup \/ Holidays-2/);
    assert.match(chosen, /Documents\/Taxes[\s\S]*Keepsake \/ Backup \/ Taxes/);
    assert.match(await page.locator("#f-destline").innerText(), /Copied into iCloud Drive \/ Keepsake \/ Backup\./);
    assert.equal((await page.locator("#f-add").innerText()), "Add 3 folders");
    await page.fill("#f-name", "HolidayPhotos");
    assert.match(await page.locator("#f-destline").innerText(), /Keepsake \/ HolidayPhotos\./);
    await page.click("#f-add");
    await page.waitForSelector("#toast:not([hidden])");
    assert.match(await page.locator("#toast").innerText(), /2 added, 1 not added/);
    const results = await page.locator(".result-list").innerText();
    assert.match(results, /Added Holidays\b/);
    assert.match(results, /Added Holidays-2/);
    assert.match(results, /Not added: sources \/ Documents\/Taxes[\s\S]*overlaps job 'documents'/);
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen[\s\S]*Documents\/Taxes/, "the refused folder stays chosen");
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const jobs = await page.locator(".job").allInnerTexts();
    assert.ok(jobs.some((t) => /Holidays[ -]2/.test(t)) && jobs.some((t) => /Holidays\b/.test(t)));
    await page.goto(base + "/#/job/Holidays-2");
    await page.waitForSelector("#j-keep");
    assert.match(await page.locator("main").innerText(), /In iCloud:\s*Keepsake \/ HolidayPhotos \/ Holidays-2/);
  });

  await test("add folder: an existing destination folder can be chosen; one Keepsake did not create needs a tick", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector(".picker li.prow");
    await page.waitForSelector("#f-dest option[value='HolidayPhotos']", { state: "attached" });
    const opts = await page.locator("#f-dest option").allInnerTexts();
    assert.equal(opts[0], "New folder");
    assert.ok(opts.some((o) => /Holiday Photos \(HolidayPhotos\) \(made by Keepsake\)/.test(o)));
    assert.ok(opts.some((o) => /Photos \(photos\) \(holds one folder's copy, cannot be shared\)/.test(o)));
    assert.equal(await page.locator("#f-dest option[value='photos']").isDisabled(), true);
    await tick("Scans");
    await page.selectOption("#f-dest", "HolidayPhotos");
    assert.equal(await page.locator("#f-name").isVisible(), false, "no new name when an existing folder is chosen");
    assert.match(await page.locator("#f-destline").innerText(), /Keepsake \/ HolidayPhotos\./);
    assert.equal(await page.locator("#f-ack").isVisible(), false);
    assert.equal(await page.locator("#f-add").isDisabled(), false);
    assert.match(await page.locator(".chosen").innerText(), /Keepsake \/ HolidayPhotos \/ Scans/);
  });

  await test("add folder: a new name that is taken, or not allowed, is refused before anything is sent", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector(".picker li.prow");
    await tick("Scans");
    await page.fill("#f-name", "HolidayPhotos");
    assert.equal(await page.locator("#f-add").isDisabled(), true);
    assert.match(await page.locator("#f-reason").innerText(), /already exists in iCloud Drive \/ Keepsake/);
    // the same name in another case is the same folder on a Mac
    await page.fill("#f-name", "holidayphotos");
    assert.equal(await page.locator("#f-add").isDisabled(), true);
    assert.match(await page.locator("#f-reason").innerText(), /HolidayPhotos already exists[\s\S]*differ only by case/);
    await page.fill("#f-name", "Bad Name!");
    assert.match(await page.locator("#f-reason").innerText(), /letters, digits and hyphens, with no spaces/);
    await page.fill("#f-name", "");
    assert.match(await page.locator("#f-reason").innerText(), /Type a name for the new destination folder/);
  });

  await test("add folder: a destination Keepsake did not create is refused until the person ticks the box", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector("#f-dest option[value='family-stuff']", { state: "attached" });
    await tick("Scans");
    await page.selectOption("#f-dest", "family-stuff");
    assert.equal(await page.locator("#f-ack").isVisible(), true);
    assert.match(await page.locator("#f-ack").locator("xpath=..").innerText(), /Keepsake did not create this folder/);
    assert.equal(await page.locator("#f-add").isDisabled(), true);
    assert.match(await page.locator("#f-reason").innerText(), /Tick the box to confirm you want to use a folder that Keepsake did not create/);
    await page.check("#f-ack");
    assert.equal(await page.locator("#f-add").isDisabled(), false);
    await page.selectOption("#f-dest", "");
    assert.equal(await page.locator("#f-ack").isVisible(), false);
  });

  await test("Refresh on the source list: a new folder appears without reloading, you stay in the folder, the choice is kept", async () => {
    mkdirSync(`${workdir}/sources/Scratch/one/deep`, { recursive: true });
    mkdirSync(`${workdir}/sources/Scratch/two`, { recursive: true });
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector(".picker li.prow");
    await openRow("Scratch");
    await page.waitForSelector(row("Scratch/one"));
    await tick("Scratch/one");
    assert.match(await page.locator(".picker .refresh-row").innerText(), /Updated just now/);
    mkdirSync(`${workdir}/sources/Scratch/three`);
    assert.equal(await page.locator(row("Scratch/three")).count(), 0);
    await page.click('.picker button.refresh');
    await page.waitForSelector(row("Scratch/three"));
    assert.match(await page.locator(".crumbs").innerText(), /sources\s*\/\s*Scratch/, "still inside Scratch");
    assert.equal(await page.locator(`${row("Scratch/one")} input`).isChecked(), true, "the choice is kept");
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen/);
    assert.equal(await page.locator(".picker button.refresh").isDisabled(), false);
    assert.equal(await page.locator(".picker .spin").isVisible(), false);
  });

  await test("Refresh on the source list: a chosen folder that has gone is unselected and said so; a vanished current folder steps out", async () => {
    await tick("Scratch/two");
    assert.match(await page.locator(".chosen").innerText(), /2 folders chosen/);
    rmSync(`${workdir}/sources/Scratch/two`, { recursive: true });
    await page.click('.picker button.refresh');
    await page.waitForSelector(".notices li");
    assert.match(await page.locator(".notices").innerText(), /sources \/ Scratch\/two is no longer there, so it was unselected/);
    assert.match(await page.locator(".chosen").innerText(), /1 folder chosen/);
    assert.equal(await page.locator(row("Scratch/two")).count(), 0);
    // inside a folder that is then removed: back to the nearest one still there
    await openRow("Scratch/one");
    await page.waitForSelector(row("Scratch/one/deep"));
    rmSync(`${workdir}/sources/Scratch/one`, { recursive: true });
    await page.click('.picker button.refresh');
    await page.waitForSelector(".notices li:has-text('you are back in sources / Scratch')");
    assert.match(await page.locator(".crumbs").innerText(), /Scratch/);
    // the unselecting follows the notice by a few requests: wait for it, do not race it
    await until(page, () => /No folders chosen yet/.test(document.querySelector(".chosen").innerText));
    assert.match(await page.locator(".chosen").innerText(), /No folders chosen yet/, "the folder inside the vanished one was unselected too");
    rmSync(`${workdir}/sources/Scratch`, { recursive: true });
  });

  await test("Refresh on the destination list: a folder made a moment ago appears; a chosen one that vanished is dropped in words", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector("#f-dest option[value='family-stuff']", { state: "attached" });
    assert.match(await page.locator(".destfield .refresh-row").innerText(), /Updated just now/);
    mkdirSync(`${workdir}/icloud/made-just-now`);
    assert.equal(await page.locator("#f-dest option[value='made-just-now']").count(), 0);
    await page.click('.destfield button.refresh');
    await page.waitForSelector("#f-dest option[value='made-just-now']", { state: "attached" });
    assert.match(await page.locator("#f-dest option[value='made-just-now']").innerText(), /not made by Keepsake/);
    await page.selectOption("#f-dest", "made-just-now");
    rmSync(`${workdir}/icloud/made-just-now`, { recursive: true });
    await page.click('.destfield button.refresh');
    await page.waitForSelector(".destfield p.hint:has-text('is no longer in iCloud Drive / Keepsake')");
    assert.match(await page.locator(".destfield").innerText(), /made-just-now is no longer in iCloud Drive \/ Keepsake, so New folder is selected/);
    assert.equal(await page.inputValue("#f-dest"), "");
  });

  await test("names show as readable Title Case with the real name beside them; Camel Case is accepted", async () => {
    await page.goto(base + "/#/locations");
    await page.waitForSelector(".locrow");
    const shown = await page.evaluate(() => ["NasSharedDrive", "HomeFolder", "photo-library", "PDFScans", "sources", "Backup2020"].map((n) => window.keepsake.displayName(n)));
    assert.deepEqual(shown, ["Nas Shared Drive", "Home Folder", "Photo Library", "PDF Scans", "Sources", "Backup2020"]);
    assert.equal(await page.inputValue("#f-name").catch(() => ""), "");
  });

  await test("Locations: built-in ones cannot be removed; one is added from the mounted places, read-only, and listed readably", async () => {
    await page.goto(base + "/#/locations");
    await page.waitForSelector(".locrow");
    const builtin = page.locator('.locrow[data-location="sources"]');
    assert.match(await builtin.innerText(), /Built in/);
    assert.equal(await builtin.locator("button").count(), 0, "a built-in location has no Remove");
    await page.click("#loc-add");
    await page.waitForSelector("dialog[open] li.prow");
    assert.match(await page.locator("dialog[open] .crumbs").innerText(), /Mounted places/);
    await page.click("dialog[open] li.prow button.open >> nth=0");     // the mounted place
    await page.waitForSelector('dialog[open] li.prow:has-text("HomeFolder")');
    assert.equal(await page.locator("#loc-do-add").isDisabled(), true);
    await page.check('dialog[open] li.prow:has-text("HomeFolder") input');
    assert.match(await page.locator('dialog[open] .chosen').innerText(), /1 folder chosen/);
    assert.equal(await page.inputValue('dialog[open] .locname'), "HomeFolder");
    await page.click("#loc-do-add");
    await page.waitForSelector("dialog[open] .result-list li.ok");
    assert.match(await page.locator("dialog[open] .result-list").innerText(), /Added HomeFolder/);
    await page.keyboard.press("Escape");
    await page.waitForSelector('.locrow[data-location="HomeFolder"]');
    const added = await page.locator('.locrow[data-location="HomeFolder"]').innerText();
    assert.match(added, /Home Folder/);
    assert.match(added, /HomeFolder/);
    assert.equal(await page.locator('.locrow[data-location="HomeFolder"] button').count(), 1, "an added location can be removed");
  });

  await test("Locations: a name that differs only by case is refused; a location in use cannot be removed and the job is named", async () => {
    const csrf = (await (await page.context().request.get(base + "/api/session")).json()).csrf;
    const post = (path, data) => page.context().request.post(base + path, { headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, data });
    const first = await (await page.context().request.get(base + "/api/locations/browse?path=")).json();
    const prefix = first.folders[0].path;
    const clash = await post("/api/locations", { name: "homefolder", path: `${prefix}/ExternalDrive` });
    assert.equal(clash.status(), 400);
    assert.match((await clash.json()).message, /'HomeFolder' already exists[\s\S]*differ only by case/);
    const job = await post("/api/jobs", { name: "DocsCopy", root: "HomeFolder", source: "Documents", auto_check: "off" });
    assert.equal(job.status(), 200, JSON.stringify(await job.json()));
    await page.goto(base + "/#/locations");
    await page.reload();
    await page.waitForSelector('.locrow[data-location="HomeFolder"] button');
    assert.match(await page.locator('.locrow[data-location="HomeFolder"]').innerText(), /used by 1 folder/);
    await page.click('.locrow[data-location="HomeFolder"] button');
    await page.click("dialog[open] button.danger");
    await page.waitForSelector("#toast:not([hidden])");
    assert.match(await page.locator("#toast").innerText(), /folder job 'DocsCopy' still keeps folders from 'HomeFolder'/);
    assert.equal(await page.locator('.locrow[data-location="HomeFolder"]').count(), 1, "still listed");
    const gone = await post("/api/jobs/DocsCopy/remove", { delete_copy: false });
    assert.equal(gone.status(), 200);
    await page.reload();
    await page.waitForSelector('.locrow[data-location="HomeFolder"] button');
    await page.click('.locrow[data-location="HomeFolder"] button');
    await page.click("dialog[open] button.danger");
    await page.waitForSelector('.locrow[data-location="HomeFolder"]', { state: "detached" });
  });

  await test("Add folder: Add location from the picker's own control, and the new location is chosen from a readable list", async () => {
    await page.goto(base + "/#/add");
    await page.reload();
    await page.waitForSelector("#f-addloc");
    assert.equal(await page.locator("#f-root option").count(), 1);
    await page.click("#f-addloc");
    await page.waitForSelector("dialog[open] li.prow");
    await page.click("dialog[open] li.prow button.open >> nth=0");
    await page.waitForSelector('dialog[open] li.prow:has-text("ExternalDrive")');
    await page.check('dialog[open] li.prow:has-text("ExternalDrive") input');
    await page.click("#loc-do-add");
    await page.waitForSelector("dialog[open] .result-list li.ok");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#f-root option[value='ExternalDrive']", { state: "attached" });
    assert.match(await page.locator("#f-root option[value='ExternalDrive']").innerText(), /External Drive \(ExternalDrive\)/);
    await page.selectOption("#f-root", "ExternalDrive");
    await page.waitForSelector('.picker li.prow[data-path="Backups"]');
  });

  await test("job settings: save a new schedule and see it again", async () => {
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#j-keep");
    await page.fill("#j-keep", "14");
    await page.click('button[type=submit]');
    await page.waitForSelector(".job");
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#j-keep");
    assert.equal(await page.inputValue("#j-keep"), "14");
  });

  await test("accepting a smaller source asks first", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    await page.click('button[aria-label="Review projects: the source shrank"]');
    await page.waitForSelector("dialog[open]");
    assert.match(await page.locator("dialog").innerText(), /Sync even though the source shrank/);
    await page.click('dialog button:has-text("Cancel")');
    await page.waitForSelector("dialog[open]", { state: "detached" });
  });

  await test("every folder says what its last run changed and when it was last checked", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const photos = page.locator(".job").first();
    assert.match(await photos.locator("td[data-label='Last run']").innerText(), /^12 added \u00b7 /);
    const shown = (loc) => loc.locator("[aria-hidden='true']").first().innerText();
    assert.match(await shown(photos.locator("td[data-label='Verified']")), /^\u2713 12\/12 \u00b7 \w+ \d+:\d+$/);
    assert.match(await photos.locator("td[data-label='Verified'] .ver").getAttribute("title"), /^12 of 12 files match, checked .+\. Next check .+\.$/);
    const documents = page.locator(".job").nth(1);
    assert.match(await documents.locator("td[data-label='Last run']").innerText(), /^1 moved to \.deleted \u00b7 /);
    assert.equal(await shown(documents.locator("td[data-label='Verified']")), "Not checked");
    assert.equal(await documents.locator("td[data-label='Verified'] .ver").getAttribute("title"), "Not checked yet. Check due now.");
    assert.equal(await documents.locator("td[data-label='Kept']").innerText(), "1");
    assert.equal(await shown(photos.locator("td[data-label='Kept']")), "\u2013", "nothing kept is a quiet dash, not a 0");
    assert.equal(await photos.locator("td[data-label='Kept'] .sr").innerText(), "None kept");
    await documents.locator("summary").click();
    assert.match(await documents.locator(".changes").innerText(), /removed: Taxes\/return-2018\.pdf/);
  });

  await test("folder rows are compact: one to two short lines, nothing wraps, nothing scrolls sideways", async () => {
    await page.goto(base + "/#/folders");
    await page.reload();      // a hash change alone keeps the page, with a folder's changed files left open by an earlier test
    await page.waitForSelector(".job");
    const m = await page.evaluate(() => {
      const wrap = document.querySelector(".tablewrap");
      return {
        rows: [...document.querySelectorAll("tr.f")].map((r) => Math.round(r.getBoundingClientRect().height)),
        lines: [...document.querySelectorAll("tr.f td.c-last, tr.f td.c-ver")].map((c) => {
          const line = parseFloat(getComputedStyle(c).lineHeight);
          return Math.round((c.querySelector(".lr") || c.querySelector(".ver")).getBoundingClientRect().height / line * 10) / 10;
        }),
        sideways: wrap.scrollWidth - wrap.clientWidth };
    });
    assert.ok(m.rows.every((h) => h <= 64), "rows are " + m.rows.join(", ") + "px tall");
    assert.ok(m.lines.every((n) => n <= 1.2), "last run and verified wrap: " + m.lines.join(", ") + " lines");
    assert.ok(m.sideways <= 0, "the table scrolls sideways by " + m.sideways + "px");
  });

  await test("the short row text keeps the whole value in its title and for screen readers", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const photos = page.locator('.job[data-job="photos"]');
    assert.match(await photos.locator("td.c-last .lr").innerText(), /^12 added \u00b7 (just now|\d+[smhd] ago)$/);
    assert.match(await photos.locator("td.c-last [title]").first().getAttribute("title"), /^12 added, .+ \(.+\)$/);
    assert.match(await photos.locator("td.c-last .sr").innerText(), /^12 added, .+ \(.+\)$/);
    assert.equal(await photos.locator(".src").getAttribute("title"), "sources / Photos");
    assert.equal(await photos.locator(".src .sr").innerText(), "sources / Photos");
    const projects = page.locator('.job[data-job="projects"]');
    assert.match(await projects.locator("td.c-last .lr").innerText(), /^Failed \u00b7 /);
    assert.equal(await projects.locator("td.c-last .tag").innerText(), "source shrank", "the failure is a short tag; the sentence is under the row");
  });

  await test("the row menu: opens from the keyboard, arrows move, Escape closes and returns to its button", async () => {
    await page.goto(base + "/#/folders");
    await page.reload();      // a hash change alone keeps the page, with whatever an earlier test left running
    await page.waitForSelector(".job");
    const btn = page.locator('.job[data-job="photos"] button.morebtn');
    const menu = page.locator('.job[data-job="photos"] [role="menu"]');
    assert.equal(await btn.getAttribute("aria-haspopup"), "menu");
    assert.equal(await btn.getAttribute("aria-expanded"), "false");
    assert.equal(await menu.isHidden(), true);
    await btn.focus();
    await page.keyboard.press("Enter");
    assert.equal(await btn.getAttribute("aria-expanded"), "true");
    assert.deepEqual(await menu.locator('[role="menuitem"]').allInnerTexts(), ["Preview", "Check", "Edit"]);
    const at = () => page.evaluate(() => document.activeElement.textContent);
    assert.equal(await at(), "Preview", "focus goes to the first item");
    await page.keyboard.press("ArrowDown"); assert.equal(await at(), "Check");
    await page.keyboard.press("ArrowDown"); assert.equal(await at(), "Edit");
    await page.keyboard.press("ArrowDown"); assert.equal(await at(), "Preview", "it wraps round");
    await page.keyboard.press("ArrowUp"); assert.equal(await at(), "Edit");
    await page.keyboard.press("Home"); assert.equal(await at(), "Preview");
    await page.keyboard.press("Escape");
    assert.equal(await btn.getAttribute("aria-expanded"), "false");
    assert.equal(await menu.isHidden(), true);
    assert.equal(await page.evaluate(() => document.activeElement.dataset.focus), "more:photos", "focus is back on the menu button");
    await btn.click();
    assert.equal(await btn.getAttribute("aria-expanded"), "true");
    await page.mouse.click(4, 500);
    assert.equal(await btn.getAttribute("aria-expanded"), "false", "a click elsewhere closes it");
    await btn.focus();
    await page.keyboard.press("ArrowDown");
    assert.equal(await btn.getAttribute("aria-expanded"), "true", "ArrowDown opens it too");
    await page.keyboard.press("Tab");
    assert.equal(await btn.getAttribute("aria-expanded"), "false", "Tab leaves and closes it");
  });

  await test("a note under a row is one line and opens to the whole sentence", async () => {
    await page.setViewportSize({ width: 900, height: 800 });
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const note = page.locator('.job[data-job="projects"] details.notice.line.bad').first();
    const summary = note.locator("summary");
    assert.match(await summary.innerText(), /the source shrank from 20 to 5 files \(more than 20%\)/, "the whole sentence is in the page");
    assert.equal(await note.evaluate((d) => d.open), false);
    const closed = await summary.evaluate((el) => ({ h: el.getBoundingClientRect().height, cut: el.scrollWidth > el.clientWidth + 1 }));
    assert.equal(closed.cut, true, "closed, a long sentence is cut to one line");
    await summary.click();
    assert.equal(await note.evaluate((d) => d.open), true);
    const open = await summary.evaluate((el) => ({ h: el.getBoundingClientRect().height, cut: el.scrollWidth > el.clientWidth + 1 }));
    assert.equal(open.cut, false, "opened, none of it is cut");
    assert.ok(open.h > closed.h, "opened, it takes the lines it needs");
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("on a phone a row is a short card with the key facts first, and its menu stays on screen", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const g = await page.evaluate(() => {
      const r = (q) => document.querySelector('.job[data-job="photos"] ' + q).getBoundingClientRect();
      return { card: Math.round(r("tr.f").height), name: r(".fname").y, last: r("td.c-last").y, ver: r("td.c-ver").y, nights: r("td.nights").y,
        sideways: document.documentElement.scrollWidth - window.innerWidth };
    });
    assert.ok(g.name < g.last && g.last < g.ver && g.ver < g.nights, "facts before the nights: " + JSON.stringify(g));
    assert.ok(g.card <= 260, "the card is " + g.card + "px tall");
    assert.ok(g.sideways <= 0, "scrolls sideways by " + g.sideways + "px");
    await page.click('.job[data-job="photos"] button.morebtn');
    const box = await page.locator('.job[data-job="photos"] [role="menu"]').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.y >= 0 && box.y + box.height <= 844, "the menu is inside the screen: " + JSON.stringify(box));
    const heights = await page.locator('.job[data-job="photos"] [role="menuitem"]').evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
    assert.ok(heights.every((h) => h >= 44), "menu items are tap-sized: " + heights);
    await page.evaluate(() => window.scrollBy(0, 30));      // the menu follows its button instead of closing
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.job[data-job="photos"] button.morebtn').getAttribute("aria-expanded"), "true", "scrolling keeps it open");
    assert.ok((await page.locator('.job[data-job="photos"] [role="menu"]').boundingBox()).y < box.y, "and it moved with the button");
    await page.keyboard.press("Escape");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("check a copy: reads it back, says so in plain words, and a difference needs attention", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    await rowAction(page, "documents", "Check a copy of documents");
    await page.waitForSelector('.job[data-job="documents"] td[data-label="Verified"]:has-text("5 of 5")');
    assert.match(await page.locator("#toast").innerText(), /Checked 5 files, all match/);
    // Same size, other bytes, in the iCloud copy: a content difference only a hash sees.
    writeFileSync(`${workdir}/icloud/documents/Taxes/return-2020.pdf`, Buffer.alloc(9000, "z"));
    await rowAction(page, "documents", "Check a copy of documents");
    await page.waitForSelector('.job.s-failed[data-job="documents"]:has-text("different content in iCloud")');
    const card = page.locator('.job[data-job="documents"]');
    assert.match(await card.innerText(), /Needs attention/);
    assert.equal(await card.locator("td[data-label='Verified'] [aria-hidden='true']").first().innerText(), "\u2717 1 differ");
    assert.match(await card.locator("td[data-label='Verified'] .ver").getAttribute("title"), /^1 of 5 files differ, checked /);
    assert.equal(await card.locator("td[data-label='Verified'] .ver.bad").count(), 1, "a difference is plainly red");
    assert.match(await card.innerText(), /'Taxes\/return-2020\.pdf' has different content in iCloud/);
    assert.match(await page.locator("#banner").innerText(), /needs attention: .*documents/);
  });

  await test("deleted files: listed read-only, restore goes into .restored after a confirmation", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    await page.click('#nav a:has-text("Deleted files")');
    await page.waitForSelector('a[aria-label="Deleted files of documents"]');
    assert.equal(await page.locator("#nav a[aria-current=page]").innerText(), "Deleted files");
    await page.click('a[aria-label="Deleted files of documents"]');
    await page.waitForSelector("ul.log li");
    const row = await page.locator("ul.log li").first().innerText();
    assert.match(row, /Taxes\/return-2018\.pdf/);
    assert.match(row, /8\.8 KB, 30 days left/);
    assert.match(await page.locator("main").innerText(), /Your original files are never written/);
    await page.click('button[aria-label^="Restore Taxes/return-2018.pdf"]');
    await page.waitForSelector("dialog[open]");
    assert.match(await page.locator("dialog").innerText(), /goes into \.restored\/.* source is not touched/);
    await page.click('dialog button:has-text("Cancel")');
    await page.waitForSelector("dialog[open]", { state: "detached" });
    await page.click('button[aria-label^="Restore Taxes/return-2018.pdf"]');
    await page.waitForSelector("dialog[open]");
    await page.click('dialog button:has-text("Restore")');
    await page.waitForSelector('#toast:has-text("restored a copy")');
    assert.match(await page.locator("#toast").innerText(), /restored a copy to \.restored\/\d{4}-\d{2}-\d{2}\/Taxes\/return-2018\.pdf/);
  });

  await test("deleted files are grouped by date, newest first, each with its own count and Restore all", async () => {
    await page.goto(base + "/#/deleted/documents");
    await page.waitForSelector("section.group");
    const groups = page.locator("section.group");
    assert.equal(await groups.count(), 2);
    const dates = await groups.evaluateAll((els) => els.map((e) => e.dataset.date));
    assert.deepEqual(dates, [...dates].sort().reverse());
    assert.match(await groups.nth(0).innerText(), /Kept on \d{4}-\d{2}-\d{2}\s+1 file, 8\.8 KB/);
    assert.match(await groups.nth(1).innerText(), /2 files, 8\.1 KB/);
    assert.match(await groups.nth(1).innerText(), /Taxes\/notes\.txt[\s\S]*Taxes\/return-2017\.pdf/);
    assert.equal(await page.locator('section.group button:has-text("Restore all from this date")').count(), 2);
    assert.equal(await page.locator("#restore-everything").innerText(), "Restore everything kept");
  });

  await test("restore all from a date: the dialog states the count and size, cancel does nothing, confirm copies and says what happened", async () => {
    await page.goto(base + "/#/deleted/documents");
    await page.waitForSelector("section.group");
    const older = await page.locator("section.group").nth(1).getAttribute("data-date");
    const dest = `${workdir}/icloud/documents/.restored/${older}`;
    await page.click(`button[aria-label="Restore all from ${older}"]`);
    await page.waitForSelector("dialog[open]");
    const text = await page.locator("dialog").innerText();
    assert.match(text, /Restore from \d{4}-\d{2}-\d{2}\?/);
    assert.match(text, /copies 2 files \(8\.1 KB\) into \.restored\/\d{4}-\d{2}-\d{2}/);
    assert.match(text, /Nothing is overwritten/);
    assert.match(text, /source is not touched/);
    await page.click('dialog button:has-text("Cancel")');
    await page.waitForSelector("dialog[open]", { state: "detached" });
    assert.equal(existsSync(dest), false);
    await page.click(`button[aria-label="Restore all from ${older}"]`);
    await page.waitForSelector("dialog[open]");
    await page.click('dialog button:has-text("Restore 2 files")');
    await page.waitForSelector('#restore-result:has-text("Restored 2 files")');
    assert.match(await page.locator("#restore-result").innerText(), /into \.restored\/\d{4}-\d{2}-\d{2} inside the iCloud copy of 'documents'.*source was not touched/);
    assert.equal(readFileSync(`${dest}/Taxes/notes.txt`, "utf8"), "n".repeat(300));
    assert.equal(existsSync(`${workdir}/icloud/documents/.deleted/${older}/Taxes/notes.txt`), true);
  });

  await test("restore everything kept: counts every date, never replaces a file that is already restored", async () => {
    await page.goto(base + "/#/deleted/documents");
    await page.waitForSelector("#restore-everything");
    const older = await page.locator("section.group").nth(1).getAttribute("data-date");
    await page.click("#restore-everything");
    await page.waitForSelector("dialog[open]");
    assert.match(await page.locator("dialog").innerText(), /Restore everything kept\?[\s\S]*copies 3 files \(16\.9 KB\) into \.restored\/<date>, one folder per date/);
    await page.click('dialog button:has-text("Restore 3 files")');
    await page.waitForSelector('#restore-result:has-text("Restored 3 files")');
    assert.match(await page.locator("#restore-result").innerText(), /\(2 dates\)/);
    // Two of the three were already restored a moment ago: they get a number, the first copies stay as they were.
    assert.equal(readFileSync(`${workdir}/icloud/documents/.restored/${older}/Taxes/notes.txt`, "utf8"), "n".repeat(300));
    assert.equal(readFileSync(`${workdir}/icloud/documents/.restored/${older}/Taxes/notes (2).txt`, "utf8"), "n".repeat(300));
    assert.equal(existsSync(`${workdir}/icloud/documents/.restored/${older}/Taxes/return-2017 (2).pdf`), true);
  });

  await test("a batch the server refuses shows its sentence, never a success", async () => {
    const csrf = (await (await ctx.request.get(base + "/api/session")).json()).csrf;
    const post = (data) => ctx.request.post(base + "/api/jobs/documents/restore-batch", {
      headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" }, data });
    for (const data of [{}, { date: "../.." }, { date: "2020-01-01" }, { all: true, expect_files: 99 }]) {
      const r = await post(data);
      const body = await r.json();
      assert.equal(r.status(), 400, JSON.stringify(data));
      assert.equal(body.ok, false);
      assert.ok(body.message.length > 10);
    }
  });

  await test("evicted files: a plain count with what it means for a restore, and the honest line about what Keepsake can see", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const photos = page.locator('.job[data-job="photos"]');
    assert.match(await photos.locator("details.evicted summary").innerText(), /^3 files evicted by macOS Optimize Storage$/);
    await photos.locator("details.evicted summary").click();
    assert.match(await photos.locator("details.evicted p").innerText(), /not on this Mac's disk[\s\S]*a restore from this folder needs them downloaded first/);
    assert.equal(await page.locator('.job[data-job="documents"] details.evicted').count(), 0);
    assert.doesNotMatch(await page.locator("main").innerText(), /\buploaded to iCloud\b/);
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#j-auto");
    assert.match(await page.locator(".notice.evicted").innerText(), /3 files evicted by macOS Optimize Storage/);
    await page.goto(base + "/#/settings/help");
    await page.waitForSelector("#panel-help");
    assert.match(await page.locator("#panel-help").innerText(), /\.photo\.jpg\.icloud/);
  });

  await test("check a copy automatically: weekly for a new folder, saved per folder, last and next check shown", async () => {
    await page.goto(base + "/#/add");
    await page.waitForSelector("#f-auto");
    assert.equal(await page.inputValue("#f-auto"), "weekly");
    assert.deepEqual(await page.locator("#f-auto option").allInnerTexts(), ["Off", "Daily", "Weekly", "Monthly"]);
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#j-auto");
    assert.equal(await page.inputValue("#j-auto"), "weekly");
    assert.match(await page.locator(".checkline").innerText(), /Last check: Checked 12 files, all match.*Next check .*Every file has been looked at after about 1 check/);
    await page.selectOption("#j-auto", "monthly");
    await page.fill("#j-stale", "5");
    await page.click('button[type=submit]');
    await page.waitForSelector(".job");
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#j-auto");
    assert.equal(await page.inputValue("#j-auto"), "monthly");
    assert.equal(await page.inputValue("#j-stale"), "5");
    await page.selectOption("#j-auto", "off");
    await page.click('button[type=submit]');
    await page.waitForSelector(".job");
    assert.match(await page.locator('.job[data-job="photos"] td[data-label="Verified"] .sr').innerText(), /Automatic check off\.$/);
    assert.match(await page.locator('.job[data-job="music"] td[data-label="Verified"] .sr').innerText(), /Paused\.$/);
  });

  await test("settings: every alert channel says only whether it is set, and a test with none set up says so", async () => {
    await page.goto(base + "/#/settings/alerts");
    await page.waitForSelector("#test-alert");
    const rows = await page.locator('table[aria-label="Alert channels"] tbody tr').allInnerTexts();
    assert.deepEqual(rows.map((r) => r.split("\t")[0]), ["webhook", "ntfy", "pushover", "email", "mqtt"]);
    assert.ok(rows.every((r) => /Not set/.test(r)));
    assert.match(await page.locator("main").innerText(), /never here[\s\S]*not shown by this page or by the API/);
    assert.equal(await page.locator("#s-ev-stale").count(), 1);
    await page.click("#test-alert");
    await page.waitForSelector("#test-out .notice");
    assert.match(await page.locator("#test-out").innerText(), /no alert channel is set up/);
    assert.equal(await page.locator("#test-out .notice.bad").count(), 1);
    const dl = page.locator("a#diagnostics");
    assert.equal(await dl.getAttribute("href"), "/api/diagnostics");
    assert.equal(await dl.getAttribute("download"), "keepsake-diagnostics.txt");
    assert.match(await dl.innerText(), /Download diagnostics/);
  });

  const selectedTab = async (pg) => pg.locator('[role="tab"][aria-selected="true"]').innerText();
  const visiblePanels = async (pg) => pg.locator('[role="tabpanel"]:not([hidden])').count();

  await test("settings tabs: a labelled tab bar, one panel at a time, the address remembers the tab", async () => {
    await page.goto(base + "/#/settings");
    await page.waitForSelector('[role="tablist"]');
    assert.deepEqual(await page.locator('[role="tab"]').allInnerTexts(), ["Limits", "Alerts", "Pause", "Locations", "Diagnostics", "Help"]);
    assert.equal(await selectedTab(page), "Limits");
    assert.equal(await visiblePanels(page), 1);
    for (const tab of await page.locator('[role="tab"]').all()) {
      const panel = page.locator("#" + (await tab.getAttribute("aria-controls")));
      assert.equal(await panel.getAttribute("role"), "tabpanel");
      assert.equal(await panel.getAttribute("aria-labelledby"), await tab.getAttribute("id"));
    }
    await page.click("#tab-alerts");
    assert.equal(await selectedTab(page), "Alerts");
    assert.equal(await visiblePanels(page), 1);
    assert.match(await page.locator("#panel-alerts").innerText(), /Webhook address/);
    assert.equal(await page.locator("#panel-limits").isHidden(), true);
    assert.match(page.url(), /#\/settings\/alerts$/);
    await page.reload();
    await page.waitForSelector('[role="tablist"]');
    assert.equal(await selectedTab(page), "Alerts", "a reload comes back to the same tab");
    await page.goto(base + "/#/settings/locations");
    await page.waitForSelector('[role="tablist"]');
    assert.equal(await selectedTab(page), "Locations", "a deep link opens its tab");
    await page.waitForSelector('#panel-locations .locrow[data-location="sources"]');
    assert.match(await page.locator("#panel-locations").innerText(), /iCloud Drive folder[\s\S]*The places Keepsake may read from/);
    assert.equal(await page.locator("#panel-locations #loc-add").count(), 1, "the locations card lives in this tab");
    await page.goto(base + "/#/settings/nonsense");
    await page.waitForSelector('[role="tablist"]');
    assert.equal(await selectedTab(page), "Limits", "an unknown tab falls back to the first");
    assert.match(page.url(), /#\/settings\/limits$/);
    await page.goto(base + "/#/settings/diagnostics");
    await page.waitForSelector("a#diagnostics");
    assert.equal(await page.locator("a#diagnostics").isVisible(), true);
  });

  await test("help tab: the first-run help lives in Settings, grouped under short headings, and the old address leads there", async () => {
    await page.goto(base + "/#/settings/help");
    await page.waitForSelector("#panel-help");
    assert.equal(await selectedTab(page), "Help");
    assert.equal(await visiblePanels(page), 1);
    assert.deepEqual(await page.locator("#panel-help h2").allInnerTexts(),
      ["Getting started", "Locations and folders", "Destination folder", "Copies and checks", "Restore and deleted files", "Alerts", "Password", "Troubleshooting"]);
    const text = await page.locator("#panel-help").innerText();
    assert.match(text, /reset-password/);
    assert.match(text, /guided setup/);
    assert.doesNotMatch(text, /Apple has uploaded|Honest limits/);
    await page.goto(base + "/#/help");
    await page.waitForSelector("#panel-help");
    assert.match(page.url(), /#\/settings\/help$/, "the old Help address redirects");
    assert.equal(await selectedTab(page), "Help");
    assert.equal(await page.locator('#nav a[href="#/help"]').count(), 0, "no standalone Help page in the menu");
    await page.reload();
    await page.waitForSelector("#panel-help");
    assert.equal(await selectedTab(page), "Help", "a reload comes back to the Help tab");
  });

  await test("settings tabs: arrow keys, Home and End move between tabs, one tab stop", async () => {
    await page.goto(base + "/#/settings");
    await page.waitForSelector('[role="tablist"]');
    await page.focus("#tab-limits");
    const stops = await page.locator('[role="tab"]').evaluateAll((els) => els.map((e) => e.tabIndex));
    assert.deepEqual(stops, [0, -1, -1, -1, -1, -1], "only the open tab is in the tab order");
    const press = async (key, want) => {
      await page.keyboard.press(key);
      assert.equal(await selectedTab(page), want, key);
      assert.equal(await page.evaluate(() => document.activeElement.id), "tab-" + want.toLowerCase(), key + " moves focus too");
    };
    await press("ArrowRight", "Alerts");
    await press("ArrowRight", "Pause");
    await press("ArrowLeft", "Alerts");
    await press("End", "Help");
    await press("ArrowRight", "Limits");
    await press("ArrowLeft", "Help");
    await press("Home", "Limits");
    assert.equal(await visiblePanels(page), 1);
  });

  await test("settings tabs: nothing typed is lost on a tab switch or a redraw, and the page says so", async () => {
    await page.goto(base + "/#/settings");
    await page.waitForSelector("#s-max");
    const before = await page.inputValue("#s-max");
    assert.equal(await page.locator(".unsaved:visible").count(), 0, "no hint while nothing changed");
    await page.fill("#s-max", "123");
    assert.match(await page.locator(".unsaved:visible").first().innerText(), /unsaved changes in Limits/);
    assert.match(await page.locator("#tab-limits").getAttribute("class"), /dirty/);
    assert.match(await page.locator("#tab-limits").getAttribute("aria-label"), /unsaved changes/);
    await page.click("#tab-alerts");
    await page.fill("#s-hook", "https://example.com/not-saved");
    assert.match(await page.locator(".unsaved:visible").first().innerText(), /Limits and Alerts/, "the hint follows into the other tab");
    await page.click("#tab-limits");
    assert.equal(await page.inputValue("#s-max"), "123", "still there after switching back");
    await page.click("#tab-pause");
    await page.click("#p-1h");                       // redraws the whole page
    await page.waitForSelector("#p-resume");
    assert.equal(await selectedTab(page), "Pause", "the redraw stays on the same tab");
    await page.click("#p-resume");
    await page.waitForSelector("#p-resume", { state: "detached" });
    await page.click("#tab-limits");
    assert.equal(await page.inputValue("#s-max"), "123", "a redraw keeps unsaved edits");
    await page.click("#tab-alerts");
    assert.equal(await page.inputValue("#s-hook"), "https://example.com/not-saved");
    await page.fill("#s-hook", "");
    // Save settings saves both tabs together, from whichever tab it is pressed in
    await page.click("#panel-alerts button[type=submit]");
    await page.waitForSelector(".unsaved:visible", { state: "detached" });
    assert.equal(await page.locator("#tab-limits.dirty").count(), 0);
    await page.reload();
    await page.waitForSelector('[role="tablist"]');
    await page.click("#tab-limits");
    assert.equal(await page.inputValue("#s-max"), "123", "saved from the other tab");
    await page.fill("#s-max", before);               // put the fixture back
    await page.click("#panel-limits button[type=submit]");
    await page.waitForSelector(".unsaved:visible", { state: "detached" });
    await page.reload();
    await page.waitForSelector('[role="tablist"]');
    assert.equal(await page.inputValue("#s-max"), before);
    // leaving Settings drops an unsaved edit: it must not come back days later
    await page.fill("#s-max", "77");
    await page.goto(base + "/#/folders");
    await page.waitForSelector("main h1");
    await page.goto(base + "/#/settings");
    await page.waitForSelector("#s-max");
    assert.equal(await page.inputValue("#s-max"), before);
    assert.equal(await page.locator(".unsaved:visible").count(), 0);
  });

  await test("settings tabs on a phone: the bar scrolls inside itself, the page does not", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + "/#/settings/diagnostics");
    await page.waitForSelector('[role="tablist"]');
    await page.waitForTimeout(150);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no sideways scroll of the page");
    const tab = await page.locator("#tab-diagnostics").boundingBox();
    assert.ok(tab.x >= 0 && tab.x + tab.width <= 390, "the open tab is scrolled into view");
    assert.ok(tab.height >= 44, "a tap target");
    await page.focus("#tab-diagnostics");
    await page.keyboard.press("Home");
    await page.waitForTimeout(150);
    const first = await page.locator("#tab-limits").boundingBox();
    assert.ok(first.x >= 0, "Home brings the first tab back into view");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("a restore the engine refuses shows its sentence", async () => {
    const csrf = (await (await ctx.request.get(base + "/api/session")).json()).csrf;
    const r = await ctx.request.post(base + "/api/jobs/documents/restore", {
      headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
      data: { date: "2026-11-07", path: "../../.keepsake-owner" } });
    assert.equal(r.status(), 400);
    assert.match((await r.json()).message, /not allowed/);
  });

  await test("activity shows what changed on the last run next to the log", async () => {
    await page.goto(base + "/#/activity");
    await page.waitForSelector(".log li strong");
    // the log lines arrive after the first card: wait for them, do not race them
    await until(page, () => /ok in [\d.]+s: 12 added/.test(document.querySelector("main").innerText));
    const text = await page.locator("main").innerText();
    assert.match(text, /What changed on the last run/);
    assert.match(text, /photos[\s\S]*Last run: 12 added/);
    assert.match(text, /ok in [\d.]+s: 12 added/);
  });

  await test("activity: one line per entry, grouped by day, problems stay loud, the filter lives in the address", async () => {
    // A log with a few days of routine runs and some problems, written where the service reads it.
    const file = `${workdir}/data/activity.jsonl`;
    const original = readFileSync(file, "utf8");
    const long = "failed: 12 files changed while being read, so iCloud holds an older version or none; they are retried next run (exclude files that always change)";
    const lines = [];
    const now = Date.now();
    for (let i = 119; i >= 0; i--) {
      const bad = i % 40 === 0;
      lines.push(JSON.stringify({ at: new Date(now - (i * 40 + 2) * 60000).toISOString(), level: bad ? "error" : "info",
        job: i % 2 ? "photos" : "documents", message: bad ? long : `ok in ${i % 9 + 1}.2s: ${i} added` }));
    }
    writeFileSync(file, original + lines.join("\n") + "\n");
    try {
      await page.goto(base + "/#/folders");           // leave the page so it reads the log again
      await page.goto(base + "/#/activity");
      await until(page, () => document.querySelectorAll(".alog-row").length >= 50);
      // paged: the first 50, then 50 more on request
      assert.equal(await page.locator(".alog-row").count(), 50);
      await page.click("#alog-more");
      assert.equal(await page.locator(".alog-row").count(), 100);
      // compact: a routine line is one short row
      assert.match(await page.locator(".alog-row .alog-time").first().innerText(), /^\d\d:\d\d$/);   // 24-hour, whatever the locale
      const h0 = (await page.locator(".alog-row.ok .alog-line").first().boundingBox()).height;
      assert.ok(h0 <= 36, "a routine row is " + h0 + "px tall");
      // grouped by day, newest first, with plain names for the last two days
      const heads = await page.locator(".alog-head").allInnerTexts();
      assert.match(heads[0], /^Today/);
      assert.match(heads[1], /^Yesterday/);
      assert.ok(heads.length >= 3, "three days of entries: " + heads.join(" | "));
      // an error is one line too, with a control that opens its whole sentence
      await page.goto(base + "/#/activity/problems");
      await page.waitForSelector(".alog-row.bad");
      assert.equal(await page.locator(".alog-row:not(.bad)").count(), 0);
      const line = page.locator(".alog-row.bad .alog-line").first();
      const msg = line.locator(".alog-msg");
      assert.equal(await line.getAttribute("aria-expanded"), "false");
      assert.ok(await msg.evaluate((e) => e.scrollWidth > e.clientWidth), "the long sentence is cut to one line");
      assert.match(await line.locator(".sr").innerText(), /Problem/);
      await line.focus();
      await page.keyboard.press("Enter");
      assert.equal(await line.getAttribute("aria-expanded"), "true");
      assert.ok(await msg.evaluate((e) => e.scrollWidth <= e.clientWidth + 1 && e.getBoundingClientRect().height > 20), "opened, the whole sentence shows");
      assert.match(await page.locator(".alog-row.bad.open .alog-full").first().innerText(), /\d/);
      await page.keyboard.press("Enter");
      assert.equal(await line.getAttribute("aria-expanded"), "false");
      // the filter and the folder are in the address and come back on a reload
      assert.equal(new URL(page.url()).hash, "#/activity/problems");
      await page.selectOption("#alog-folder", "documents");
      assert.equal(new URL(page.url()).hash, "#/activity/problems/documents");
      assert.deepEqual([...new Set(await page.locator(".alog-row .alog-tag").allInnerTexts())], ["documents"]);
      await page.goto(base + "/#/activity");
      await page.goto(base + "/#/activity/problems/documents");
      await page.waitForSelector(".alog-row");
      assert.equal(await page.locator('.alog-seg button[aria-pressed="true"]').innerText(), "Problems only");
      assert.equal(await page.locator("#alog-folder").inputValue(), "documents");
      // a day folds away its routine lines, never its problems
      await page.goto(base + "/#/activity");
      await page.waitForSelector(".alog-day");
      const day = page.locator(".alog-day").first();
      await day.locator(".alog-head button").click();
      assert.equal(await day.locator(".alog-head button").getAttribute("aria-expanded"), "false");
      assert.equal(await day.locator(".alog-row.ok").first().isVisible(), false);
      assert.equal(await day.locator(".alog-row.bad").first().isVisible(), true);
      // a phone: no sideways scroll, a message wraps to two lines at most
      await page.setViewportSize({ width: 390, height: 844 });
      try {
        await page.goto(base + "/#/activity");
        await page.waitForSelector(".alog-row");
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), "no horizontal scroll");
        const tall = await page.locator(".alog-row:not(.open) .alog-msg").evaluateAll((els) => Math.max(...els.map((e) => e.getBoundingClientRect().height / parseFloat(getComputedStyle(e).lineHeight))));
        assert.ok(tall <= 2.1, "a message is " + tall + " lines tall");
      } finally {
        await page.setViewportSize({ width: 1280, height: 800 });
      }
    } finally {
      writeFileSync(file, original);
    }
  });

  await test("activity, settings and help pages render", async () => {
    for (const [hash, text] of [["activity", /Activity/], ["settings", /Safety limits/], ["settings/help", /only on a Mac/]]) {
      await page.goto(base + "/#/" + hash);
      await page.waitForSelector("main h1");
      assert.match(await page.locator("main").innerText(), text);
    }
    await page.goto(base + "/#/settings/help");
    assert.match(await page.locator("main").innerText(), /not affiliated with or endorsed by Apple Inc/i);
  });

  await test("theme switch: Light, Dark and Auto set data-theme, set aria-pressed and persist", async () => {
    const theme = () => page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    const pressed = () => page.locator("#theme button[aria-pressed=true]").allInnerTexts();
    const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    await page.goto(base + "/#/folders");
    await page.reload();
    await page.waitForSelector(".job");
    assert.equal(await theme(), null);
    assert.deepEqual(await pressed(), ["Auto"]);
    assert.equal(await page.locator("#theme button").count(), 3);
    await page.click('#theme button:has-text("Dark")');
    assert.equal(await theme(), "dark");
    assert.deepEqual(await pressed(), ["Dark"]);
    const dark = await bg();
    assert.equal(await page.evaluate(() => localStorage.getItem("keepsake-theme")), "dark");
    await page.reload();
    await page.waitForSelector(".job");
    assert.equal(await theme(), "dark");
    assert.deepEqual(await pressed(), ["Dark"]);
    await page.click('#theme button:has-text("Light")');
    assert.equal(await theme(), "light");
    assert.notEqual(await bg(), dark);
    await page.reload();
    assert.equal(await theme(), "light");
    // Auto follows the system again and forgets the saved choice.
    await page.click('#theme button:has-text("Auto")');
    assert.equal(await theme(), null);
    assert.equal(await page.evaluate(() => localStorage.getItem("keepsake-theme")), null);
    await page.emulateMedia({ colorScheme: "dark" });
    assert.equal(await bg(), dark);
    await page.emulateMedia({ colorScheme: "light" });
    assert.notEqual(await bg(), dark);
    await page.emulateMedia({ colorScheme: null });
  });

  await test("a saved theme is applied before the page's script runs (no flash) and storage failures are harmless", async () => {
    await page.evaluate(() => localStorage.setItem("keepsake-theme", "dark"));
    const early = await ctx.newPage();
    // app.js is held back, so only the head script can have set the theme.
    await early.route("**/app.js", (r) => r.abort());
    await early.goto(base + "/#/folders");
    await early.waitForLoadState("domcontentloaded");
    assert.equal(await early.evaluate(() => document.documentElement.getAttribute("data-theme")), "dark");
    await early.close();
    await page.evaluate(() => localStorage.removeItem("keepsake-theme"));
    const blocked = await browser.newContext();
    await blocked.addInitScript(() => { Object.defineProperty(window, "localStorage", { get() { throw new Error("blocked"); } }); });
    const bp = await blocked.newPage();
    const errs = [];
    bp.on("pageerror", (e) => errs.push(String(e)));
    await bp.goto(base + "/#/folders");
    await bp.waitForSelector(".job");
    await bp.click('#theme button:has-text("Dark")');
    assert.equal(await bp.evaluate(() => document.documentElement.getAttribute("data-theme")), "dark");
    assert.deepEqual(errs, []);
    await blocked.close();
  });

  await test("the menu is rendered once: one nav, at the top, and none after the content", async () => {
    for (const hash of ["folders", "activity", "deleted", "settings", "help", "add"]) {
      await page.goto(base + "/#/" + hash);
      await page.waitForSelector("main h1");
      assert.equal(await page.locator("nav").count(), 1, hash);
      assert.equal(await page.locator("#nav a").count(), 4, hash);
      assert.deepEqual(await page.locator("#nav a").allInnerTexts(), ["Folders", "Activity", "Deleted files", "Settings"]);
      assert.equal(await page.locator("header.top nav").count(), 1);
      const below = await page.evaluate(() => {
        const main = document.querySelector("main").getBoundingClientRect().bottom;
        return [...document.querySelectorAll("nav, nav a")].filter((n) => n.getBoundingClientRect().top >= main).length;
      });
      assert.equal(below, 0, hash);
    }
  });

  // WCAG contrast of two computed CSS colours ("rgb(...)", "rgba(...)" or "color(srgb ...)").
  const channels = (c) => {
    const m = /^rgba?\(([^)]+)\)$/.exec(c) || /^color\(srgb ([^)]+)\)$/.exec(c);
    const v = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return c.startsWith("color(") ? v.slice(0, 3).map((x) => x * 255) : v.slice(0, 3);
  };
  const luminance = (c) => channels(c).map((x) => { const u = x / 255; return u <= 0.03928 ? u / 12.92 : ((u + 0.055) / 1.055) ** 2.4; })
    .reduce((acc, x, i) => acc + x * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const navStyle = (loc) => loc.evaluate((el) => { const cs = getComputedStyle(el); return { color: cs.color, bg: cs.backgroundColor, weight: Number(cs.fontWeight), underline: cs.borderBottomColor, outline: cs.outlineStyle, outlineWidth: cs.outlineWidth }; });

  await test("every page lights its own menu item, shows its own heading and title, and sub-pages keep their section lit", async () => {
    const routes = [
      ["folders", "Folders", "Folders", /^Folders · Keepsake$/, null],
      ["add", "Folders", "Add folder", /^Add folder · Keepsake$/, "Back to Folders"],
      ["job/photos", "Folders", "photos", /^photos · Folder settings · Keepsake$/, "Back to Folders"],
      ["activity", "Activity", "Activity", /^Activity · Keepsake$/, null],
      ["activity/problems", "Activity", "Activity", /^Activity · Keepsake$/, null],
      ["deleted", "Deleted files", "Deleted files", /^Deleted files · Keepsake$/, null],
      ["deleted/documents", "Deleted files", "Deleted files", /^Deleted files · documents · Keepsake$/, "Back to Deleted files"],
      ["settings", "Settings", "Settings", /^Settings · Keepsake$/, null],
      ["settings/help", "Settings", "Settings", /^Settings · Keepsake$/, null],
      ["locations", "Settings", "Locations", /^Locations · Keepsake$/, "Back to Settings"],
      ["help", "Settings", "Settings", /^Settings · Keepsake$/, null],
    ];
    for (const [hash, section, h1, title, back] of routes) {
      await page.goto(base + "/#/" + hash);
      await page.reload();
      await page.waitForSelector("main h1");
      const lit = await page.locator('#nav a[aria-current="page"]').allInnerTexts();
      assert.deepEqual(lit, [section], `${hash}: exactly one item is marked, and it is ${section}`);
      assert.equal(await page.locator("main h1").count(), 1, hash);
      assert.equal((await page.locator("main h1").innerText()).trim(), h1, hash);
      assert.match(await page.title(), title, hash);
      if (back) assert.equal((await page.locator("main a.back").first().innerText()).replace(/^←\s*/, ""), back, hash);
      else assert.equal(await page.locator("main a.back").count(), 0, hash);
    }
  });

  await test("the page you are on is unmistakable: bold, accent underline, filled; hover and focus look different; AA contrast in light and dark", async () => {
    await page.goto(base + "/#/activity");
    await page.waitForSelector("main h1");
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      const here = await navStyle(page.locator('#nav a[aria-current="page"]'));
      const other = await navStyle(page.locator('#nav a:not([aria-current])').first());
      const pageBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      assert.ok(here.weight >= 700 && other.weight <= 500, `${scheme}: bold only where you are (${here.weight}/${other.weight})`);
      assert.notEqual(here.bg, "rgba(0, 0, 0, 0)", `${scheme}: the current item is filled`);
      assert.equal(other.bg, "rgba(0, 0, 0, 0)", `${scheme}: the others are not`);
      assert.notEqual(here.underline, other.underline, `${scheme}: the underline marks it`);
      assert.ok(contrast(here.color, here.bg) >= 4.5, `${scheme}: current item contrast ${contrast(here.color, here.bg).toFixed(2)}`);
      assert.ok(contrast(other.color, pageBg) >= 4.5, `${scheme}: other items contrast ${contrast(other.color, pageBg).toFixed(2)}`);
      assert.ok(contrast(here.color, pageBg) > contrast(other.color, pageBg), `${scheme}: the current item is stronger than the rest`);
      // hover: a quiet grey underline, not the accent: hover must never read as "you are here"
      const folders = page.locator('#nav a[data-page="folders"]');
      await folders.hover();
      const hovered = await navStyle(folders);
      assert.notEqual(hovered.underline, here.underline, `${scheme}: hover is not the accent underline`);
      assert.notEqual(hovered.underline, other.underline, `${scheme}: hover shows something`);
      assert.equal(hovered.bg, "rgba(0, 0, 0, 0)", `${scheme}: hover is not filled`);
      await page.mouse.move(0, 400);
    }
    await page.emulateMedia({ colorScheme: null });
    // keyboard focus: a visible ring on a menu item (skip link, brand, then the items)
    await page.reload();
    await page.waitForSelector("main h1");
    await page.locator("body").press("Tab");
    for (let i = 0; i < 6 && !(await page.evaluate(() => document.activeElement?.closest("#nav") !== null)); i++) await page.keyboard.press("Tab");
    const focused = await navStyle(page.locator("#nav a:focus"));
    assert.notEqual(focused.outline, "none", "a focused menu item shows a ring");
    assert.equal(focused.outlineWidth, "3px");
    // Settings tabs read the same way: the selected tab is filled and underlined, the others are plain
    await page.goto(base + "/#/settings/alerts");
    await page.waitForSelector('[role="tab"][aria-selected="true"]');
    const tab = await navStyle(page.locator('[role="tab"][aria-selected="true"]'));
    const otherTab = await navStyle(page.locator('[role="tab"][aria-selected="false"]').first());
    assert.notEqual(tab.bg, "rgba(0, 0, 0, 0)");
    assert.equal(otherTab.bg, "rgba(0, 0, 0, 0)");
    assert.notEqual(tab.underline, otherTab.underline);
  });

  await test("the brand is the real icon inline beside the wordmark: one link to Folders; icons are linked and nothing is fetched from elsewhere", async () => {
    await page.goto(base + "/#/settings");
    await page.waitForSelector("main h1");
    const brand = page.locator("header.top a.brand");
    assert.equal(await brand.getAttribute("aria-label"), "Keepsake, Folders");
    assert.equal(await brand.getAttribute("href"), "#/folders");
    assert.equal(await brand.locator("svg.logo").count(), 1, "the logo is inline, not an image request");
    assert.equal(await brand.locator("img").count(), 0);
    const box = await brand.locator("svg.logo").boundingBox();
    assert.ok(Math.abs(box.width - 28) < 1 && Math.abs(box.height - 28) < 1, `28px logo (${box.width}x${box.height})`);
    assert.equal((await brand.locator(".wordmark").innerText()).trim(), "Keepsake");
    assert.equal(await brand.locator(".wm-sub").isVisible(), true, "the lighter 'for iCloud' shows on wide screens");
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".brand .wordmark")).fontWeight), "700");
    const bar = await page.locator("header.top").boundingBox();
    assert.ok(bar.height <= 64, `the bar stays compact (${bar.height}px)`);
    assert.equal(await page.locator('link[rel="icon"][href="/favicon.svg"]').count(), 1);
    assert.equal(await page.locator('link[rel="icon"][href="/favicon-32.png"]').count(), 1);
    assert.equal(await page.locator('link[rel="apple-touch-icon"][href="/touch-icon.png"]').count(), 1);
    await brand.click();
    await page.waitForSelector(".job");
    assert.equal(new URL(page.url()).hash, "#/folders");
    assert.deepEqual(external, [], "no request left the page's own origin");
    await page.setViewportSize({ width: 390, height: 844 });
    try {
      assert.equal(await brand.locator(".wm-sub").isVisible(), false, "'for iCloud' is hidden on a phone");
      assert.equal(await brand.locator(".wordmark").isVisible(), true);
      const small = await brand.locator("svg.logo").boundingBox();
      assert.ok(Math.abs(small.width - 24) < 1, `24px logo on a phone (${small.width})`);
    } finally {
      await page.setViewportSize({ width: 1280, height: 800 });
    }
  });

  await test("on a phone the active menu item stays in view and no page scrolls sideways", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    try {
      for (const hash of ["folders", "add", "job/photos", "activity", "deleted", "deleted/documents", "settings", "locations"]) {
        await page.goto(base + "/#/" + hash);
        await page.reload();
        await page.waitForSelector("main h1");
        const m = await page.evaluate(() => {
          const here = document.querySelector('#nav a[aria-current="page"]').getBoundingClientRect();
          const nav = document.getElementById("nav").getBoundingClientRect();
          const de = document.documentElement;
          return { left: here.left, right: here.right, navLeft: nav.left, navRight: nav.right, scroll: de.scrollWidth, client: de.clientWidth };
        });
        assert.ok(m.left >= m.navLeft - 1 && m.right <= m.navRight + 1, `${hash}: the current item is inside the menu (${JSON.stringify(m)})`);
        assert.equal(m.scroll, m.client, `${hash}: no horizontal page scroll`);
      }
    } finally {
      await page.setViewportSize({ width: 1280, height: 800 });
    }
  });

  await test("keyboard: skip link first, every control has a name; targets are 44px on a phone", async () => {
    await page.goto(base + "/#/folders");
    await page.reload();
    await page.waitForSelector(".job");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.className), "skip");
    const audit = () => page.evaluate(() => {
      const out = [], small = [];
      document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (!el.checkVisibility() || r.width === 0 || r.height === 0 || r.right <= 0) return;   // hidden, or the skip link parked off screen
        const box = el.type === "checkbox" ? el.closest("label").getBoundingClientRect() : r;
        const name = (el.getAttribute("aria-label") || el.innerText || el.labels?.[0]?.innerText || el.getAttribute("title") || "").trim();
        // A night bar is 14 to a row: on a phone 44 tall and at least 24 wide (WCAG 2.5.8), never 44 wide.
        if (el.classList.contains("nb")) { if (box.width < 24 || box.height < 43.5) small.push("small night bar: " + name); }
        else if (Math.min(box.width, box.height) < 43.5) small.push("small: " + (name || el.tagName));
        if (!name) out.push("unnamed: " + el.outerHTML.slice(0, 60));
      });
      return { out, small };
    });
    assert.deepEqual((await audit()).out, []);
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.setViewportSize({ width: 390, height: 844 });
    for (const hash of ["folders", "deleted", "deleted/documents", "add", "job/photos", "settings"]) {
      await page.goto(base + "/#/" + hash);
      await page.waitForSelector("main h1");
      await page.waitForTimeout(150);
      await page.evaluate(() => document.activeElement && document.activeElement.blur());
      const r = await audit();
      assert.deepEqual(r, { out: [], small: [] }, hash);
    }
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("phone layout: stacked rows with the strip under the folder name and a compact top bar", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const first = page.locator('.job[data-job="photos"]');
    assert.equal(await first.locator("thead").count(), 0);
    const name = await first.locator(".fname").boundingBox();
    const strip = await first.locator(".strip").boundingBox();
    assert.ok(strip.y >= name.y + name.height - 1, "strip sits under the folder name");
    assert.ok(strip.x < 60, "strip starts at the left edge of the row");
    const top = await page.locator("header.top").boundingBox();
    assert.ok(top.height < 130, `top bar is compact (${top.height}px)`);
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("phone width: no horizontal scroll on any page", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    for (const hash of ["folders", "add", "job/photos", "deleted", "deleted/documents", "activity", "settings", "help"]) {
      await page.goto(base + "/#/" + hash);
      await page.waitForSelector("main h1");
      await page.waitForTimeout(150);
      const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(over <= 0, `${hash} scrolls sideways by ${over}px`);
    }
  });


  await test("a night opens its detail: outcome, counts and example files, keyboard operable, focus comes back, nothing shifts", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const strip = page.locator('.job[data-job="photos"] .strip');
    const layout = () => page.evaluate(() => {
      const r = (q) => { const b = document.querySelector(q).getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; };
      return { strip: r('.job[data-job="photos"] .strip'), main: r("main"), width: document.documentElement.scrollWidth };
    });
    const before = await layout();
    const today = strip.locator(".nb").last();
    await today.click();
    await page.waitForSelector("dialog.sheet[open]");
    const text = await page.locator("dialog.sheet").innerText();
    assert.match(text, /photos, /);
    assert.match(text, /Copied/);
    assert.match(text, /Added\s+12/);
    assert.match(text, /Updated\s+0/);
    assert.match(text, /Moved to \.deleted\s+0/);
    assert.equal(await page.locator("dialog.sheet ul.changes li").count(), 12);
    assert.match(await page.locator("dialog.sheet ul.changes li").first().innerText(), /^added: 2025\/Holidays\/IMG_\d{4}\.jpg$/);
    assert.deepEqual(await layout(), before, "opening a night must not move anything");
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog.sheet[open]", { state: "detached" });
    assert.deepEqual(await layout(), before, "closing a night must not move anything");
    await until(page, (key) => document.activeElement.dataset.focus === key, `night:photos:${await today.getAttribute("data-day")}`);
    // Enter and Space both open it from the keyboard, Escape closes it, focus returns to the same bar.
    for (const key of ["Enter", "Space"]) {
      await page.keyboard.press(key);
      await page.waitForSelector("dialog.sheet[open]");
      assert.equal(await page.evaluate(() => document.getElementById("dlg").contains(document.activeElement)), true, "focus moves into the dialog");
      await page.keyboard.press("Escape");
      await page.waitForSelector("dialog.sheet[open]", { state: "detached" });
      await until(page, () => document.activeElement.className.includes("nb"));   // focus is back on the bar
    }
  });

  await test("reopening a night in the same breath as closing it keeps the sheet open and styled", async () => {
    // The browser delivers a dialog's close event after the dialog may already be open again (Enter straight after
    // Escape). That event belongs to the earlier open and must not tidy up the new one.
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    await page.locator('.job[data-job="photos"] .strip .nb').last().click();
    await page.waitForSelector("dialog.sheet[open]");
    await page.evaluate(() => {
      const dlg = document.getElementById("dlg");
      dlg.close();                                                              // its close event is queued, not yet delivered
      document.querySelector('.job[data-job="photos"] .strip .nb:last-child').click();   // opens it again before that event runs
    });
    await new Promise((r) => setTimeout(r, 200));                               // let the stale close event be delivered
    const state = await page.evaluate(() => { const d = document.getElementById("dlg"); return { open: d.open, sheet: d.classList.contains("sheet") }; });
    assert.deepEqual(state, { open: true, sheet: true }, "the reopened night must stay open and keep its sheet styling");
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog.sheet[open]", { state: "detached" });
    await until(page, () => document.activeElement.className.includes("nb"));
  });

  await test("nights recorded before details existed say so, a night with no run says so, a refused night says why", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const night = (job, i) => page.locator(`.job[data-job="${job}"] .strip .nb`).nth(i);
    await night("photos", 0).click();                   // seeded as copied, recorded before details
    await page.waitForSelector("dialog.sheet[open]");
    assert.match(await page.locator("dialog.sheet").innerText(), /No detail recorded for this night/);
    assert.equal(await page.locator("dialog.sheet ul.changes").count(), 0);
    await page.keyboard.press("Escape");
    await night("photos", 3).click();                   // a day without a run
    await page.waitForSelector("dialog.sheet[open]");
    assert.match(await page.locator("dialog.sheet").innerText(), /recorded no run for this folder on this day/);
    await page.keyboard.press("Escape");
    await night("photos", 5).click();                   // a seeded night that has detail
    await page.waitForSelector("dialog.sheet[open]");
    assert.match(await page.locator("dialog.sheet").innerText(), /Added\s+\d+[\s\S]*Changed files/);
    assert.match(await page.locator("dialog.sheet ul.changes li").first().innerText(), /^added: Photos\/2025\/file-05-0\.txt$/);
    await page.keyboard.press("Escape");
    await page.locator('.job[data-job="projects"] .strip .nb').last().click();   // refused by the shrink guard
    await page.waitForSelector("dialog.sheet[open]");
    const refused = await page.locator("dialog.sheet").innerText();
    assert.match(refused, /Paused or failed/);
    assert.match(refused, /shrank from 20 to 5 files/);
    assert.match(refused, /Added\s+0/);
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog.sheet[open]", { state: "detached" });
    await until(page, () => document.activeElement.className.includes("nb"));   // let the close finish before the next test
  });

  await test("the strip is one tab stop and arrow keys move along the nights", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const strip = page.locator('.job[data-job="music"] .strip');
    await strip.locator(".nb").last().focus();
    const idx = () => page.evaluate(() => [...document.activeElement.parentNode.children].indexOf(document.activeElement));
    assert.equal(await idx(), 13);
    await page.keyboard.press("ArrowLeft");
    assert.equal(await idx(), 12);
    await page.keyboard.press("Home");
    assert.equal(await idx(), 0);
    await page.keyboard.press("End");
    assert.equal(await idx(), 13);
    await page.keyboard.press("ArrowRight");
    assert.equal(await idx(), 13, "stops at the end");
    await page.keyboard.press("ArrowLeft");
    // The focused night is the strip's one tab stop, wherever focus moved.
    const stops = await page.evaluate(() => {
      const el = document.activeElement;
      return { self: el.getAttribute("tabindex"), others: [...el.parentNode.children].filter((b) => b !== el && b.getAttribute("tabindex") !== "-1").length };
    });
    assert.deepEqual(stops, { self: "0", others: 0 });
  });

  await test("on a phone a night opens as a bottom sheet and the strip fits the row with 44px-tall bars", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const bars = await page.locator('.job[data-job="photos"] .strip .nb').evaluateAll((els) => els.map((e) => { const b = e.getBoundingClientRect(); return [Math.round(b.width * 10) / 10, Math.round(b.height)]; }));
    assert.ok(bars.every(([w, h]) => w >= 24 && h === 44), JSON.stringify(bars[0]));
    await page.locator('.job[data-job="photos"] .strip .nb').last().click();
    await page.waitForSelector("dialog.sheet[open]");
    await page.waitForTimeout(450);
    const box = await page.locator("dialog.sheet").boundingBox();
    assert.deepEqual([Math.round(box.x), Math.round(box.width)], [0, 390]);
    assert.ok(Math.abs(box.y + box.height - 844) <= 1, `sheet sits on the bottom edge (${box.y + box.height})`);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog.sheet[open]", { state: "detached" });
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  await test("preview next sync: reads, says it is a preview, and writes nothing anywhere", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    const before = snapshot(workdir);
    assert.ok(Object.keys(before).length > 50);
    await rowAction(page, "photos", "Preview the next sync of photos");
    await page.waitForSelector("dialog[open] h2:has-text('Preview of photos')");
    const text = await page.locator("dialog").innerText();
    assert.match(text, /Preview\s+Nothing was changed: not iCloud, not the source, not Keepsake's records/);
    assert.match(text, /A sync started now would find nothing new/);
    assert.match(text, /Files in the source\s+12/);
    assert.match(text, /It does not copy or read anything back/);
    assert.deepEqual(snapshot(workdir), before, "the iCloud folder, state, manifests, jobs and log are byte for byte the same");
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog[open]", { state: "detached" });
    await until(page, () => document.activeElement.dataset.focus === "more:photos");   // focus returns to the row's menu button, the Preview item being in a closed menu
    assert.deepEqual(snapshot(workdir), before);
  });

  await test("preview of a folder never copied: counts what would be added, creates nothing in iCloud, works on a paused folder", async () => {
    const before = snapshot(workdir);
    assert.equal(existsSync(`${workdir}/icloud/music`), false);
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    await rowAction(page, "music", "Preview the next sync of music");
    await page.waitForSelector("dialog[open] h2:has-text('Preview of music')");
    const text = await page.locator("dialog").innerText();
    assert.match(text, /Would be added\s+1/);
    assert.match(text, /added: Albums\/a\.mp3/);
    assert.match(text, /safe first run/);
    assert.equal(existsSync(`${workdir}/icloud/music`), false, "a preview does not create the iCloud folder");
    await page.keyboard.press("Escape");
    assert.deepEqual(snapshot(workdir), before);
  });

  await test("a refused sync offers a preview: what accepting the smaller source would move, and no sync is started by it", async () => {
    const posts = [];
    const spy = (r) => { if (r.method() === "POST" && /\/sync$/.test(r.url())) posts.push(r.url()); };
    page.on("request", spy);
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    // Whatever an earlier dialog was answered, Escape never confirms a new one (the page clears the answer when a dialog opens; Chromium also does it itself, so this pins the behaviour rather than a bug).
    await page.evaluate(() => { document.getElementById("dlg").returnValue = "yes"; });
    await page.click('button[aria-label="Review projects: the source shrank"]');
    await page.waitForSelector("dialog[open]");
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog[open]", { state: "detached" });
    await page.waitForTimeout(300);
    assert.deepEqual(posts, [], "Escape must not confirm");
    await page.click('button[aria-label="Review projects: the source shrank"]');
    await page.waitForSelector("dialog[open]");
    assert.deepEqual(await page.locator("dialog .btn").allInnerTexts(), ["Cancel", "Preview first", "Sync anyway"]);
    await page.click('dialog button:has-text("Preview first")');
    await page.waitForSelector("dialog[open] h2:has-text('Preview of projects')");
    const text = await page.locator("dialog").innerText();
    assert.match(text, /Would be moved to \.deleted\s+15/);
    assert.match(text, /The shrink guard would refuse this run without 'accept shrink': the source shrank from 20 to 5 files/);
    assert.equal(await page.locator("dialog ul.changes li").count(), 15);
    assert.deepEqual(await page.locator("dialog .btn").allInnerTexts(), ["Close", "Sync anyway"]);
    await page.click('dialog button:has-text("Close")');
    await page.waitForSelector("dialog[open]", { state: "detached" });
    // Without accepting, the same preview says it would be refused, in the run's own sentence.
    await rowAction(page, "projects", "Preview the next sync of projects");
    await page.waitForSelector("dialog[open] h2:has-text('Preview of projects')");
    const refused = await page.locator("dialog").innerText();
    assert.match(refused, /A sync started now would be refused by the shrink guard\. the source shrank from 20 to 5 files .*Nothing was changed/);
    assert.deepEqual(await page.locator("dialog .btn").allInnerTexts(), ["Close", "Preview accepting the smaller source"]);
    await page.keyboard.press("Escape");
    assert.deepEqual(posts, [], "a preview never starts a sync");
    page.off("request", spy);
  });

  await test("the folder page has Sync now, Preview next sync and Check a copy", async () => {
    await page.goto(base + "/#/job/photos");
    await page.waitForSelector("#job-preview");
    assert.deepEqual(await page.locator("#job-sync, #job-preview, #job-check").allInnerTexts(), ["Sync now", "Preview next sync", "Check a copy"]);
    await page.click("#job-preview");
    await page.waitForSelector("dialog[open] h2:has-text('Preview of photos')");
    await page.keyboard.press("Escape");
    await page.waitForSelector("dialog[open]", { state: "detached" });
    await until(page, () => document.activeElement.id === "job-preview");
    await page.goto(base + "/#/job/music");
    await page.waitForSelector("#job-preview");
    assert.equal(await page.locator("#job-sync").isDisabled(), true, "a paused folder cannot be synced ...");
    assert.equal(await page.locator("#job-preview").isDisabled(), false, "... but can be previewed");
  });

  await test("no first-run checklist once all three steps are true", async () => {
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    assert.equal(await page.locator(".checklist").count(), 0);
  });

  await test("pause everything: presets, a custom time, resume, and the chip in the top bar", async () => {
    await page.goto(base + "/#/settings/pause");
    await page.waitForSelector("#p-1h");
    assert.equal(await page.locator("#pause-state").innerText(), "Nothing is paused.");
    assert.equal(await page.locator("#pausechip").isHidden(), true);
    assert.equal(await page.locator("#p-resume").count(), 0);
    assert.deepEqual(await page.locator("#p-1h, #p-tomorrow, #p-1w").allInnerTexts(), ["1 hour", "Until tomorrow 08:00", "1 week"]);
    await page.click("#p-1h");
    await page.waitForSelector("#p-resume");
    assert.match(await page.locator("#pause-state").innerText(), /Everything is paused until .*Scheduled syncs and checks do not start.*already running is not interrupted.*Sync now and Preview still work/);
    const chip = page.locator("#pausechip");
    assert.match(await chip.innerText(), /^Paused until /);
    assert.equal(await chip.getAttribute("href"), "#/settings/pause");
    assert.match(await chip.getAttribute("aria-label"), /Paused until .*Open Settings\.$/);
    await page.goto(base + "/#/folders");
    await page.waitForSelector(".job");
    assert.match(await chip.innerText(), /^Paused until /, "visible on every page");
    assert.match(await page.locator("main .lead").innerText(), /Paused until /);
    await chip.click();
    await page.waitForSelector("#p-resume");
    await page.click("#p-resume");
    await page.waitForSelector("#p-resume", { state: "detached" });
    assert.equal(await chip.isHidden(), true);
    assert.equal(await page.locator("#pause-state").innerText(), "Nothing is paused.");
    // a custom time (read in the server's time zone) and a time already past
    const when = new Date(Date.now() + 2 * 86400000), pad = (n) => String(n).padStart(2, "0");
    const stamp = (d) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    await page.fill("#p-custom", stamp(new Date(Date.now() - 3600000)));
    await page.click("#p-custom-go");
    await page.waitForSelector('#toast:has-text("already passed")');
    assert.equal(await chip.isHidden(), true, "a refused pause pauses nothing");
    await page.fill("#p-custom", stamp(when));
    await page.click("#p-custom-go");
    await page.waitForSelector("#p-resume");
    assert.match(await chip.innerText(), /^Paused until /);
    await page.click("#p-resume");
    await page.waitForSelector("#p-resume", { state: "detached" });
  });

  await test("quiet hours: saved, shown as a chip while inside the window, off again", async () => {
    await page.goto(base + "/#/settings/pause");
    await page.waitForSelector("#q-save");
    assert.equal(await page.locator("#q-on").isChecked(), false);
    const hour = new Date().getUTCHours(), pad = (n) => String((n + 24) % 24).padStart(2, "0");
    await page.check("#q-on");
    await page.fill("#q-start", `${pad(hour - 1)}:00`);
    await page.fill("#q-end", `${pad(hour + 1)}:00`);
    await page.click("#q-save");
    await page.waitForSelector("#pausechip:not([hidden])");
    assert.match(await page.locator("#pausechip").innerText(), /^Quiet hours until /);
    await page.reload();
    await page.waitForSelector("#q-save");
    assert.equal(await page.locator("#q-on").isChecked(), true);
    assert.equal(await page.inputValue("#q-start"), `${pad(hour - 1)}:00`);
    assert.match(await page.locator("#pause-state").innerText(), /^Quiet hours until /);
    await page.uncheck("#q-on");
    await page.click("#q-save");
    await page.waitForSelector("#pausechip", { state: "hidden" });
    await page.waitForSelector('#pause-state:has-text("Nothing is paused.")');   // the page redraws after the save
    assert.equal(await page.locator("#pause-state").innerText(), "Nothing is paused.");
  });

  await test("the paused chip fits a phone: no sideways scroll and the bar stays compact", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + "/#/settings/pause");
    await page.waitForSelector("#p-1w");
    await page.click("#p-1w");
    await page.waitForSelector("#p-resume");
    for (const hash of ["folders", "settings", "job/photos"]) {
      await page.goto(base + "/#/" + hash);
      await page.waitForSelector("main h1");
      await page.waitForTimeout(150);
      const chip = await page.locator("#pausechip").boundingBox();
      assert.ok(chip.x >= 0 && chip.x + chip.width <= 390, `${hash}: chip inside the screen`);
      assert.ok(chip.height >= 28, "readable target");
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${hash}: no sideways scroll`);
      assert.ok((await page.locator("header.top").boundingBox()).height < 200);
    }
    await page.goto(base + "/#/settings/pause");
    await page.waitForSelector("#p-resume");
    await page.click("#p-resume");
    await page.waitForSelector("#p-resume", { state: "detached" });
    await page.setViewportSize({ width: 1280, height: 800 });
  });


  // ---- progress: one bar for every long operation, fed by the server's own state ----
  // The status is the real one with one folder's work faked, so what the page does with
  // each shape of progress is tested without waiting for a slow share.
  const detail = (over) => ({ op: "preview", phase: "reading-source", label: "Reading your folder", reading: true,
    files: 3420, bytes: 5.2 * 1024 ** 3, total_files: null, total_bytes: null, estimated: false, percent: null,
    path: "2019/05", elapsed_s: 42, idle_s: 1, stalled: false, can_cancel: false, cancelling: false, ...over });
  const fake = { on: false, op: "preview", detail: detail({}) };
  const workOf = (j) => Object.assign(j, { operation: fake.op, previewing: fake.op === "preview", progress_detail: fake.detail });
  await page.route((u) => u.pathname === "/api/status", async (route) => {
    const res = await route.fetch(); const body = await res.json();
    if (fake.on) workOf(body.jobs.find((j) => j.name === "photos"));
    await route.fulfill({ response: res, json: body });
  });
  await page.route((u) => u.pathname === "/api/jobs/photos" && true, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch(); const body = await res.json();
    if (fake.on) workOf(body);
    await route.fulfill({ response: res, json: body });
  });
  // A hash change alone does not reload the page: load it fresh so each test starts from the server.
  const show = async (hash) => { await page.goto(base + hash); await page.reload(); };
  const bar = () => page.locator('[data-progress="photos"] [role="progressbar"]');

  await test("progress: reading shows a moving bar with live counters, the phase in words and the folder it is in", async () => {
    Object.assign(fake, { on: true, op: "preview", detail: detail({}) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    assert.equal(await bar().getAttribute("aria-busy"), "true");
    assert.equal(await bar().getAttribute("aria-valuenow"), null, "no number is invented when no total is known");
    assert.equal(await bar().getAttribute("aria-valuemin"), "0");
    const text = await page.locator('[data-progress="photos"]').innerText();
    assert.match(text, /Reading your folder/);
    assert.match(text, /Preview \(nothing is changed\)/);
    assert.match(text, /Read 3,420 files · 5\.2 GB · 00:42/);
    assert.match(text, /Now in: 2019\/05/);
    assert.equal(await page.locator('[data-progress="photos"] .notice').count(), 1);
    assert.equal(await page.locator('[data-progress="photos"] .notice').isHidden(), true, "no stall message while it is moving");
  });

  await test("progress: a known total gives a determinate bar with aria-valuenow, and a copy says copied of N", async () => {
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync", phase: "copying", label: "Copying", reading: false,
      files: 100, total_files: 200, bytes: 50 * 1024 ** 2, total_bytes: 100 * 1024 ** 2, percent: 50 }) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    assert.equal(await bar().getAttribute("aria-valuenow"), "50");
    assert.equal(await bar().getAttribute("aria-busy"), null);
    assert.match(await page.locator('[data-progress="photos"]').innerText(), /Copied 100 of 200 files · 50\.0 MB of 100 MB/);
  });

  await test("progress in a row: the bar shows in the row's own block and the columns do not move", async () => {
    fake.on = false;
    await show("/#/folders");
    await page.waitForSelector(".job");
    const geo = () => page.evaluate(() => {
      const tr = document.querySelector('.job[data-job="photos"] tr.f');
      return { h: Math.round(tr.getBoundingClientRect().height), xs: [...tr.children].map((c) => Math.round(c.getBoundingClientRect().x)) };
    });
    const before = await geo();
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync", phase: "copying", label: "Copying", reading: false,
      files: 100, total_files: 200, bytes: 50 * 1024 ** 2, total_bytes: 100 * 1024 ** 2, percent: 50 }) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    assert.equal(await page.locator('.job[data-job="photos"] [data-progress="photos"] [role="progressbar"]').count(), 1, "the bar is in the row's own block");
    const during = await geo();
    assert.ok(Math.abs(during.h - before.h) <= 2, `the row stays ${before.h}px (${during.h}px)`);
    before.xs.forEach((x, i) => assert.ok(Math.abs(during.xs[i] - x) <= 2, `column ${i} moved from ${x} to ${during.xs[i]}`));
  });

  await test("an open row menu is not redrawn away: the new state is drawn when it closes", async () => {
    fake.on = false;
    await show("/#/folders");
    await page.waitForSelector(".job");
    const more = page.locator('.job[data-job="photos"] button.morebtn');
    await more.click();
    assert.equal(await more.getAttribute("aria-expanded"), "true");
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync", phase: "copying", label: "Copying", reading: false,
      files: 100, total_files: 200, bytes: 50 * 1024 ** 2, total_bytes: 100 * 1024 ** 2, percent: 50 }) });
    await page.waitForTimeout(6500);      // longer than the quiet poll
    assert.equal(await page.locator('.job[data-job="photos"] button.morebtn').getAttribute("aria-expanded"), "true", "still open after the page looked again");
    assert.equal(await page.locator('[data-progress="photos"]').count(), 0, "nothing was drawn under the open menu");
    await page.keyboard.press("Escape");
    await page.waitForSelector('[data-progress="photos"]', { timeout: 4000 });
  });

  await test("progress: no news for 30 seconds says so plainly; after five minutes it offers to stop and the stop is sent", async () => {
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync", idle_s: 45, stalled: true }) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"] .notice:visible');
    assert.match(await page.locator('[data-progress="photos"] .notice').innerText(),
      /No progress for 45 seconds\. A slow network share can pause for a while; this is not an error yet\./);
    assert.equal(await page.locator('[data-progress="photos"] button:has-text("Stop this run")').count(), 0, "no stop offered yet");
    fake.detail = detail({ op: "sync", idle_s: 400, stalled: true, can_cancel: true });
    await page.waitForSelector('[data-progress="photos"] button:has-text("Stop this run")', { timeout: 8000 });
    assert.match(await page.locator('[data-progress="photos"] .notice').innerText(), /Nothing has moved for 7 minutes\. Check that the NAS is switched on/);
    const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/jobs/photos/cancel"));
    const cancelUrl = (u) => u.pathname === "/api/jobs/photos/cancel";
    const cancelStub = (route) => route.fulfill({ json: { ok: true, message: "stopping 'photos'" } });
    await page.route(cancelUrl, cancelStub);
    await page.click('[data-progress="photos"] button:has-text("Stop this run")');
    await sent;
    await page.unroute(cancelUrl, cancelStub);
  });

  await test("progress: the screen reader line is a polite live region and the bar is named", async () => {
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync" }) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    assert.equal(await page.locator('[data-progress="photos"] [aria-live="polite"]').count(), 1);
    assert.match(await bar().getAttribute("aria-label"), /^Copy of photos$/);
    assert.match(await page.locator('[data-progress="photos"] [aria-live="polite"]').innerText(), /Reading your folder\. Read 3,420 files/);
  });

  await test("progress: a check shows files compared, and the folder's own page shows it too", async () => {
    Object.assign(fake, { on: true, op: "check", detail: detail({ op: "check", phase: "checking", label: "Comparing files with the originals", reading: false,
      files: 12, total_files: 40, bytes: 3 * 1024 ** 2, percent: 30 }) });
    await show("/#/job/photos");
    await page.waitForSelector('[data-progress="photos"]');
    assert.match(await page.locator('[data-progress="photos"]').innerText(), /Compared 12 of 40 files/);
    assert.equal(await page.locator("#job-sync").isDisabled(), true, "buttons follow the server's state");
    assert.equal(await bar().getAttribute("aria-label"), "Check of photos");
  });

  await test("progress: a restore shows files and bytes restored on the deleted-files page", async () => {
    Object.assign(fake, { on: true, op: "restore", detail: detail({ op: "restore", phase: "copying", label: "Restoring files", reading: false,
      files: 3, total_files: 10, bytes: 2 * 1024 ** 2, total_bytes: 8 * 1024 ** 2, percent: 25 }) });
    await show("/#/deleted/photos");
    await page.waitForSelector('[data-progress="photos"]');
    assert.match(await page.locator('[data-progress="photos"]').innerText(), /Restoring files/);
    assert.match(await page.locator('[data-progress="photos"]').innerText(), /Restored 3 of 10 files · 2\.0 MB of 8\.0 MB/);
    assert.equal(await bar().getAttribute("aria-valuenow"), "25");
  });

  await test("progress: a key-up the browser never delivered does not freeze the page (the server is what decides)", async () => {
    Object.assign(fake, { on: true, op: "preview", detail: detail({}) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    await page.keyboard.down("Meta"); await page.keyboard.down("Shift"); await page.keyboard.down("4");   // a screenshot shortcut eats the key-ups
    fake.on = false;                                                  // the server has finished
    await page.waitForSelector('[data-progress="photos"]', { state: "detached", timeout: 8000 });
    await page.keyboard.up("4"); await page.keyboard.up("Shift"); await page.keyboard.up("Meta");
  });

  await test("progress: a reload mid-run brings the bar back from the server, and a finished run shows a plain summary", async () => {
    Object.assign(fake, { on: true, op: "sync", detail: detail({ op: "sync", phase: "copying", label: "Copying", reading: false, files: 1, total_files: 4, percent: 25 }) });
    await show("/#/folders");
    await page.waitForSelector('[data-progress="photos"]');
    await page.reload();
    await page.waitForSelector('[data-progress="photos"]');
    assert.equal(await bar().getAttribute("aria-valuenow"), "25");
    fake.on = false;
    await page.waitForSelector('[data-progress="photos"]', { state: "detached", timeout: 8000 });
    assert.match(await page.locator('.job:has-text("photos")').first().innerText(), /Up to date|added|Nothing new|ago|Not run/);
  });
  fake.on = false;

  await test("nothing was requested from another site and the console is clean", async () => {
    assert.deepEqual(external, []);
    assert.deepEqual(problems, []);
  });
  await ctx.close();

  // A channel that fails: the test alert says which, in plain words, without the address.
  const hooked = await fixture(["--webhook", "http://127.0.0.1:9/secret-token-xyz"]);
  try {
    const c4 = await browser.newContext();
    const p4 = await c4.newPage();
    await test("settings: a test alert on a channel that fails says so, plainly, and never shows the address", async () => {
      await p4.goto(hooked.base + "/#/settings/alerts");
      await p4.waitForSelector("#test-alert");
      assert.match(await p4.locator('table[aria-label="Alert channels"] tbody tr').first().innerText(), /webhook\s+Configured/);
      await p4.click("#test-alert");
      await p4.waitForSelector("#test-out .notice.bad");
      const out = await p4.locator("#test-out").innerText();
      assert.match(out, /webhook failed: could not reach the server/);
      assert.match(out, /webhook\s+Failed: could not reach the server/);
      assert.doesNotMatch(await p4.locator("main").innerText(), /secret-token-xyz/);
    });
    await c4.close();
  } finally { hooked.py.stdin.end(); }

  // First run: the iCloud folder is not confirmed yet.
  const fresh = await fixture(["--no-anchor"]);
  try {
    const c3 = await browser.newContext();
    const p3 = await c3.newPage();
    await test("first run: the page says nothing is copied until the iCloud folder is confirmed", async () => {
      await p3.goto(fresh.base + "/#/folders");
      await p3.waitForSelector("#banner .notice");
      assert.match(await p3.locator("#banner").innerText(), /Nothing is copied until you confirm the iCloud folder/);
      assert.equal(await p3.locator(".checklist li.done").count(), 0);
      assert.equal(await p3.locator('.checklist li[aria-current="step"]').getAttribute("data-step"), "confirm");
      assert.equal(await p3.locator('.checklist a[href="#/settings/locations"]').innerText(), "Open Settings");
      await p3.goto(fresh.base + "/#/settings/locations");
      await p3.reload();
      await p3.click("#confirm-icloud");
      await p3.waitForSelector("dialog[open]");
      await p3.click('dialog button:has-text("Yes, this is the right folder")');
      await p3.waitForSelector("text=Confirmed: Keepsake wrote its marker file");
      await p3.goto(fresh.base + "/#/folders");
      await p3.reload();
      await p3.waitForSelector("main h1");
      assert.equal(await p3.locator("#banner .notice").count(), 0);
      await p3.waitForSelector('.checklist li[data-step="confirm"].done');
      assert.equal(await p3.locator('.checklist li[aria-current="step"]').getAttribute("data-step"), "folder");
    });
    await c3.close();
  } finally { fresh.py.stdin.end(); }


  // The first-run checklist, from a server that has the iCloud folder confirmed but no folder yet.
  const empty = await fixture(["--no-jobs", "--live-worker"]);
  try {
    const c5 = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const p5 = await c5.newPage();
    await test("first run: a checklist replaces the empty table, ticks itself from real state and goes away when all three are true", async () => {
      await p5.goto(empty.base + "/#/folders");
      await p5.waitForSelector(".checklist");
      assert.equal(await p5.locator("table.folders").count(), 0);
      assert.equal(await p5.locator(".checklist h2").innerText(), "Get started in three steps");
      assert.equal(await p5.locator(".checklist li").count(), 3);
      assert.equal(await p5.locator(".checklist li.done").count(), 1);
      assert.equal(await p5.locator(".checklist li.done").getAttribute("data-step"), "confirm");
      assert.equal(await p5.locator('.checklist li[aria-current="step"]').getAttribute("data-step"), "folder");
      assert.equal(await p5.locator('.checklist a[href="#/add"]').innerText(), "Add a folder");
      assert.match(await p5.locator(".checklist").innerText(), /Waits for the folder in step 2/);
      assert.equal(await p5.locator('.checklist a[href="#/settings/help"]').count(), 1);
      await p5.click('.checklist a[href="#/add"]');
      await p5.waitForSelector("#f-name");
      // Nothing a person sets can tick it: add a real folder through the API and the page follows.
      const csrf = (await (await c5.request.get(empty.base + "/api/session")).json()).csrf;
      const r = await c5.request.post(empty.base + "/api/jobs", { headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
        data: { name: "photos", root: "sources", source: "Photos", auto_check: "off" } });
      assert.equal(r.status(), 200);
      await p5.goto(empty.base + "/#/folders");
      await p5.reload();
      await p5.waitForSelector(".job .pill.ok", { timeout: 20000 });      // the worker made the first copy
      assert.equal(await p5.locator(".checklist li.done").count(), 2, "a sync alone is not a check");
      assert.equal(await p5.locator(".checklist h2").innerText(), "Finish setting up");
      assert.equal(await p5.locator('.checklist li[aria-current="step"]').getAttribute("data-step"), "sync-check");
      assert.equal(await p5.locator('.checklist a[href="#/job/photos"]').innerText(), "Open photos");
      assert.equal(await p5.locator("table.folders").count(), 1, "with a folder the table shows under the checklist");
      await rowAction(p5, "photos", "Check a copy of photos");
      await p5.waitForSelector(".checklist", { state: "detached", timeout: 15000 });
      assert.match(await p5.locator('.job[data-job="photos"] td[data-label="Verified"] [aria-hidden="true"]').first().innerText(), /^\u2713 12\/12 \u00b7 /);
    });
    await test("first run: the checklist fits a phone and every link has a name", async () => {
      await c5.request.post(empty.base + "/api/jobs/photos/remove", { headers: { "X-CSRF-Token": (await (await c5.request.get(empty.base + "/api/session")).json()).csrf, "Content-Type": "application/json" }, data: {} });
      await p5.setViewportSize({ width: 390, height: 844 });
      await p5.goto(empty.base + "/#/folders");
      await p5.reload();
      await p5.waitForSelector(".checklist");
      assert.ok(await p5.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const boxes = await p5.locator(".checklist a.btn").evaluateAll((els) => els.map((e) => [e.innerText.trim(), Math.round(e.getBoundingClientRect().height)]));
      assert.deepEqual(boxes, [["Add a folder", 44]]);
      assert.match(await p5.locator(".checklist .sr").first().innerText(), /\(done\)/);
    });
    await c5.close();
  } finally { empty.py.stdin.end(); }

  // Started paused: the chip is there from the first paint, and Sync still works.
  const paused = await fixture(["--pause", "tomorrow"]);
  try {
    const c6 = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const p6 = await c6.newPage();
    await test("paused until tomorrow: the chip names the time, the summary says so, and Sync stays available", async () => {
      await p6.goto(paused.base + "/#/folders");
      await p6.waitForSelector(".job");
      assert.match(await p6.locator("#pausechip").innerText(), /^Paused until .*08:00/);
      assert.match(await p6.locator("main .lead").innerText(), /Paused until .*08:00/);
      assert.doesNotMatch(await p6.locator("main .lead").innerText(), /next sync/);
      assert.equal(await p6.locator('.job[data-job="photos"] button[aria-label="Sync photos now"]').isDisabled(), false);
      await p6.goto(paused.base + "/#/settings/pause");
      await p6.waitForSelector("#p-resume");
      assert.match(await p6.locator("#pause-state").innerText(), /^Everything is paused until /);
    });
    await c6.close();
  } finally { paused.py.stdin.end(); }

  // The guided setup: a brand-new install is only the wizard, from any address,
  // and it can be walked from the first step to the end.
  const wizardFx = await fixture(["--setup", "--no-anchor", "--live-worker"]);
  try {
    const cw = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const pw = await cw.newPage();
    const heading = async (page) => (await page.locator("#wiz-h").innerText()).trim();
    await test("setup: a new install shows only the wizard, even for a typed address, with no menu, banner or links out", async () => {
      await pw.goto(wizardFx.base + "/#/settings");
      await pw.waitForSelector(".wiz");
      assert.equal(await heading(pw), "Confirm the iCloud folder");
      assert.equal(new URL(pw.url()).hash, "#/setup");
      assert.equal(await pw.locator("#nav").isVisible(), false);
      assert.equal(await pw.locator("#banner").isVisible(), false);
      assert.equal(await pw.locator("#pausechip").isVisible(), false);
      assert.equal(await pw.locator("a.brand").getAttribute("href"), null);
      assert.equal(await pw.locator("main a[href]").count(), 0, "no link leaves the wizard");
      assert.equal(await pw.locator('.wiz-steps li[aria-current="step"]').count(), 1);
      assert.match(await pw.locator('.wiz-steps li[aria-current="step"]').innerText(), /Confirm iCloud/);
      assert.equal(await pw.evaluate(() => document.activeElement && document.activeElement.id), "wiz-h", "focus moves to the step heading");
      for (const hash of ["#/folders", "#/add", "#/activity", "#/help", "#/settings/help", "#/deleted", "#/job/photos"]) {
        await pw.goto(wizardFx.base + "/" + hash);
        await pw.reload();
        await pw.waitForSelector(".wiz");
        assert.equal(await heading(pw), "Confirm the iCloud folder", hash);
      }
      assert.equal(await pw.locator("button.btn.primary").innerText(), "Confirm the iCloud folder");
    });
    await test("setup: the server refuses everything outside the wizard with a plain sentence", async () => {
      const r = await cw.request.get(wizardFx.base + "/api/settings");
      assert.equal(r.status(), 403);
      assert.match((await r.json()).message, /finish the guided setup first/);
      assert.equal((await cw.request.get(wizardFx.base + "/api/activity")).status(), 403);
      assert.equal((await cw.request.get(wizardFx.base + "/api/status")).status(), 200);
    });
    await test("setup: the steps go in order and each waits for what it needs", async () => {
      await pw.goto(wizardFx.base + "/#/setup");
      await pw.waitForSelector(".wiz");
      await pw.click("#wiz-primary");                                       // confirm the iCloud folder
      await until(pw, () => document.getElementById("wiz-h").innerText.trim() === "Where can Keepsake read from?");
      await until(pw, () => /Sources/.test((document.querySelector(".locations") || { innerText: "" }).innerText));   // the list loads after the heading
      assert.equal(await pw.locator("#wiz-primary").isDisabled(), false, "a location is available");
      assert.match(await pw.locator(".locations").innerText(), /Sources/);
      assert.doesNotMatch(await pw.locator(".locations").innerText(), /Unavailable/);
      assert.equal(await pw.locator("#loc-add").isDisabled(), false, "a location can be added from the wizard");
      assert.equal(await pw.locator('.wiz-steps li[aria-current="step"]').count(), 1);
      assert.match(await pw.locator('.wiz-steps li[aria-current="step"]').innerText(), /Choose folders/);
      await pw.click("#wiz-primary");
      await until(pw, () => document.getElementById("wiz-h").innerText.trim() === "Choose folders and the destination folder");
      await pw.waitForSelector(".picker li.prow");
      assert.equal(await pw.locator("#wiz-add a").count(), 0, "no Cancel or Manage locations link: there is nowhere to go");
      assert.equal(await pw.locator("#wiz-primary").isDisabled(), true, "no folder yet");
      assert.match(await pw.locator("#wiz-reason").innerText(), /Add at least one folder/);
      assert.equal(await pw.locator("#wiz-primary").getAttribute("aria-describedby"), "wiz-reason");
      await pw.check('.picker li.prow[data-path="Photos"] input');
      await until(pw, () => document.getElementById("f-name").value === "Photos");
      await pw.click("#wiz-add #f-add");
      await pw.waitForSelector(".wiz-have");
      assert.match(await pw.locator(".wiz-have").innerText(), /Photos/);
      assert.equal(await pw.locator("#wiz-primary").isDisabled(), false);
    });
    await test("setup: the first copy waits for the person, and a reload or another browser resumes the same step", async () => {
      await new Promise((r) => setTimeout(r, 2500));                        // the live worker is running
      const st = await (await cw.request.get(wizardFx.base + "/api/status")).json();
      assert.equal(st.jobs[0].last_success, null, "no scheduled run may beat the preview");
      assert.equal(st.setup.step, "preview");
      await pw.click("#wiz-primary");                                       // Continue to the preview
      await until(pw, () => document.getElementById("wiz-h").innerText.trim() === "Preview the first copy");
      await pw.reload();
      await pw.waitForSelector(".wiz");
      assert.equal(await heading(pw), "Preview the first copy", "a reload resumes");
      const other = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const po = await other.newPage();
      await po.goto(wizardFx.base + "/#/folders");
      await po.waitForSelector(".wiz");
      assert.equal(await heading(po), "Preview the first copy", "another browser resumes");
      await other.close();
    });
    await test("setup: preview, first copy and check, then the finished screen once", async () => {
      assert.equal(await pw.locator("#wiz-primary").innerText(), "Preview the first copy");
      await pw.click("#wiz-primary");
      await pw.waitForSelector(".wiz-list .pill.ok", { timeout: 20000 });
      assert.match(await pw.locator(".wiz-list").innerText(), /would add 12 files/);
      assert.match(await pw.locator(".wiz-list").innerText(), /change nothing that is already in iCloud/);
      assert.equal(await pw.locator("#wiz-primary").innerText(), "Continue");
      await pw.click("#wiz-primary");
      await until(pw, () => document.getElementById("wiz-h").innerText.trim() === "Make the first copy and check it");
      assert.equal(await pw.locator("#wiz-primary").innerText(), "Start the first copy");
      await pw.click("#wiz-primary");
      // The worker may run the scheduled copy check before the person presses it:
      // either way the setup is complete only when a check has matched.
      await pw.waitForSelector('#wiz-primary:has-text("Check the copy"), #wiz-h:has-text("Setup is finished")', { timeout: 30000 });
      if ((await heading(pw)) !== "Setup is finished") await pw.click("#wiz-primary");
      await pw.waitForSelector('#wiz-h:has-text("Setup is finished")', { timeout: 30000 });
      assert.equal(await pw.locator(".wiz-steps li.done").count(), 5);
      assert.equal(await pw.locator("#nav").isVisible(), false, "still only the wizard until the person leaves it");
      await pw.click("#wiz-primary");                                       // Open Keepsake
      await pw.waitForSelector(".job");
      assert.equal(await pw.locator("#nav").isVisible(), true);
      assert.equal(await pw.evaluate(() => document.body.classList.contains("setup")), false);
      assert.equal(await pw.locator("a.brand").getAttribute("href"), "#/folders");
    });
    await test("setup: once complete it never comes back, and the whole API opens", async () => {
      await pw.goto(wizardFx.base + "/#/setup");
      await pw.reload();
      await pw.waitForSelector(".job");
      assert.equal(await pw.locator(".wiz").count(), 0);
      assert.equal(new URL(pw.url()).hash, "#/folders");
      assert.equal((await cw.request.get(wizardFx.base + "/api/settings")).status(), 200);
    });
    await cw.close();
  } finally { wizardFx.py.stdin.end(); }

  // A phone-width wizard has no sideways scroll, and an install that was set up before
  // the wizard existed is never locked.
  const wizardPhone = await fixture(["--setup", "--no-anchor"]);
  try {
    const cp = await browser.newContext({ viewport: { width: 390, height: 800 } });
    const pp = await cp.newPage();
    await test("setup: at phone width the wizard fits and the buttons are tap-sized", async () => {
      await pp.goto(wizardPhone.base + "/#/setup");
      await pp.waitForSelector(".wiz");
      assert.ok(await pp.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no horizontal scroll");
      const box = await pp.locator("#wiz-primary").boundingBox();
      assert.ok(box.height >= 44, "tap target " + box.height);
    });
    await cp.close();
  } finally { wizardPhone.py.stdin.end(); }
  await test("setup: a normal install (already set up) is never locked", async () => {
    const cn = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const pn = await cn.newPage();
    await pn.goto(base + "/#/folders");
    await pn.waitForSelector(".job");
    assert.equal(await pn.locator(".wiz").count(), 0);
    assert.equal(await pn.locator("#nav").isVisible(), true);
    await cn.close();
  });


  // The wizard: "Reading" is only ever the server's word; an idle server with no result is
  // said so, with a Retry; and the last step is two named stages under one bar.
  const stage = await fixture(["--setup", "--no-anchor", "--live-worker"]);
  try {
    const cs = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const sp = await cs.newPage();
    const call = (m, path, b) => sp.evaluate(async ([m, path, b]) => {
      const s = await (await fetch("/api/session", { credentials: "same-origin" })).json();
      const r = await fetch(path, { method: m, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": s.csrf }, body: m === "GET" ? undefined : JSON.stringify(b || {}) });
      return [r.status, await r.json().catch(() => ({}))];
    }, [m, path, b]);
    await test("setup: a preview that never started says so with a Retry, instead of showing Reading for ever", async () => {
      await sp.goto(stage.base + "/#/setup");
      await sp.waitForSelector("#wiz-h");
      assert.equal((await call("POST", "/api/icloud/confirm", {}))[0], 200);
      assert.equal((await call("POST", "/api/jobs", { name: "Holidays", root: "sources", source: "Photos", dest: "Holidays" }))[0], 200);
      await sp.reload();
      await sp.waitForSelector("#wiz-h");
      await until(sp, () => document.getElementById("wiz-h")?.innerText.trim() === "Preview the first copy");
      // The first ask is swallowed on its way to the server: nothing is queued, nothing runs.
      const previewUrl = (u) => u.pathname === "/api/jobs/Holidays/preview";
      const swallow = (route) => route.request().method() === "POST" ? route.fulfill({ json: { ok: true, message: "queued" } }) : route.continue();
      await sp.route(previewUrl, swallow);
      await sp.click("#wiz-primary");
      await sp.waitForSelector(".wiz-list .pill.bad:has-text('Did not start')", { timeout: 15000 });
      assert.match(await sp.locator(".wiz-list").innerText(), /The preview did not start\. Try again\./);
      assert.equal(await sp.locator(".wiz-list .pill.running").count(), 0, "Reading is never shown for an idle server");
      await sp.unroute(previewUrl, swallow);
      await sp.click('.wiz-list button:has-text("Retry")');
      await sp.waitForSelector(".wiz-list .pill.ok", { timeout: 30000 });
    });
    await test("setup: the last step is two named stages under one overall bar, from the server's state", async () => {
      const state = { on: false, op: "sync", pct: 50 };
      const statusUrl = (u) => u.pathname === "/api/status";
      const statusStub = async (route) => {
        const res = await route.fetch(); const body = await res.json();
        if (state.on) Object.assign(body.jobs[0], { operation: state.op, progress_detail: detail({ op: state.op, phase: state.op === "check" ? "checking" : "copying", label: state.op === "check" ? "Comparing files with the originals" : "Copying", reading: false, files: 5, total_files: 10, bytes: 5, total_bytes: 10, percent: state.pct }) });
        await route.fulfill({ response: res, json: body });
      };
      await sp.route(statusUrl, statusStub);
      await sp.click("#wiz-primary");                                    // Continue
      await until(sp, () => document.getElementById("wiz-h")?.innerText.trim() === "Make the first copy and check it");
      state.on = true;
      const wbar = sp.locator('[data-progress="Holidays"] [role="progressbar"]');
      await sp.waitForSelector('[data-progress="Holidays"]', { timeout: 8000 });
      assert.equal(await wbar.getAttribute("aria-valuenow"), "25", "half of stage 1 of 2 is a quarter overall");
      assert.match(await sp.locator('[data-progress="Holidays"]').innerText(), /Copying 1 of 2/);
      state.op = "check";
      await until(sp, () => document.querySelector('[data-progress="Holidays"]').innerText.includes("Checking 2 of 2"));
      assert.equal(await wbar.getAttribute("aria-valuenow"), "75", "half of stage 2 of 2 is three quarters overall");
      await sp.unroute(statusUrl, statusStub);
    });
    await cs.close();
  } finally { stage.py.stdin.end(); }

  // Login flow against a second server that needs a password.
  const locked = await fixture(["--password", "demo-password-123"]);
  try {
    const c2 = await browser.newContext();
    const p2 = await c2.newPage();
    await test("password: the page asks first, refuses a wrong one, accepts the right one", async () => {
      await p2.goto(locked.base + "/#/folders");
      await p2.waitForSelector("#pw");
      assert.equal(await p2.locator(".job").count(), 0);
      await p2.fill("#pw", "not-it");
      await p2.click('button[type=submit]');
      await p2.waitForSelector(".notice.bad[role=alert]:not([hidden])");
      await p2.fill("#pw", "demo-password-123");
      await p2.click('button[type=submit]');
      await p2.waitForSelector(".job");
    });
    await test("settings tabs: a Security tab holds Change password when the page has a password", async () => {
      await p2.goto(locked.base + "/#/settings");
      await p2.waitForSelector('[role="tablist"]');
      assert.deepEqual(await p2.locator('[role="tab"]').allInnerTexts(), ["Limits", "Alerts", "Pause", "Locations", "Diagnostics", "Security", "Help"]);
      assert.equal(await p2.locator("#pw-form").isHidden(), true, "not on the first tab");
      await p2.click("#tab-security");
      assert.equal(await p2.locator("#pw-form").isVisible(), true);
      assert.match(p2.url(), /#\/settings\/security$/);
      assert.equal(await p2.locator("#panel-limits").isHidden(), true);
      await p2.reload();
      await p2.waitForSelector("#pw-form");
      assert.equal(await selectedTab(p2), "Security", "a reload comes back to Security");
    });
    await test("password: logging out brings the login back", async () => {
      await p2.click("#logout");
      await p2.waitForSelector("#pw");
    });
    await c2.close();
  } finally { locked.py.stdin.end(); }
} finally {
  await browser.close();
  py.stdin.end();
}
console.log(`${passed} UI checks passed`);
