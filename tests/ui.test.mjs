import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { messages, translate, escapeHTML, progress } from "../web/i18n.mjs";

test("every wizard and guidance message exists in both languages", () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages.zh).sort());
  for (const [language, dictionary] of Object.entries(messages)) {
    for (const [key, value] of Object.entries(dictionary)) {
      assert.equal(typeof value, "string", `${language}.${key}`);
      assert.ok(value.trim(), `${language}.${key} is empty`);
      assert.deepEqual(
        [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort(),
        [...messages.en[key].matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort(),
        `Interpolation differs for ${language}.${key}`,
      );
    }
  }
  assert.throws(() => translate("en", "not-a-message"), /Missing translation/);
});

test("album names and interpolated messages cannot become executable markup", () => {
  const album = '<img src=x onerror="unexpected()"> & kid\'s music';
  const output = escapeHTML(album);
  assert.ok(!output.includes("<img"));
  assert.ok(output.includes("&lt;img"));
  assert.ok(output.includes("&amp;"));
  assert.ok(output.includes("&#39;"));
  assert.equal(translate("en", "completedCount", { done: 2, total: 10 }), "2 of 10 cards completed");
  assert.ok(translate("zh", "completedCount", { done: 2, total: 10 }).includes("2 / 10"));
});

test("progress includes skipped cards and never invents completed work", () => {
  const job = { state: "running", total: 2, completed: ["one"] };
  assert.equal(progress(job), 50);
  assert.equal(progress({ ...job, completed: ["one", "two"] }), 99);
  assert.equal(progress({ ...job, state: "stopped" }), 50);
  assert.equal(progress({ ...job, state: "completed" }), 50);
  assert.equal(progress({ ...job, skipped: [{ title: "two" }] }), 99);
  assert.equal(progress({ ...job, state: "completed", skipped: [{ title: "two" }] }), 100);
});

test("browser wizard scans automatically, enforces selection and offers a safe login fallback", async () => {
  // This is a lightweight DOM boundary stub, not a real-browser UX test.
  const original = {};
  const events = {};
  const writes = [];
  const copied = [];
  const opened = [];
  const confirmations = [];
  const storage = new Map();
  let confirm = false;
  let chosen = false;
  let jobNumber = 0;
  let poll;
  let nextJobStatus = "started";
  let holdNextConfig = false;
  let releaseConfig;
  let holdNextStatus = false;
  let releaseStatus;
  const element = {
    innerHTML: "",
    addEventListener(type, handler) { events[type] = handler; },
    querySelector() { return { disabled: false }; },
    querySelectorAll() { return []; },
  };
  const syntheticAlbum = title => ({
    title, tracks: 1, icons: 1, cover: "cover.png", coverStatus: "provided", audioIssue: null,
    trackDetails: [{ name: "One.mp3", hasIcon: true, issue: null }], unmatchedIcons: [],
  });
  const reviewResult = (title, presence = "missing", extra = {}) => ({
    title, presence, cover: "unverified", remoteTracks: presence === "present" ? 1 : null,
    remoteIcons: presence === "present" ? 1 : null, remoteCovers: presence === "present" ? 1 : null,
    pendingCreate: false, pendingOverwrite: false, tracks: [], error: null, ...extra,
  });
  const reviewReport = (id, results, scanRevision) => ({
    id, operation: "inspect", state: "completed", requested: results.map(item => item.title),
    completed: results.map(item => item.title), skipped: [], results, error: null, scanRevision,
  });
  const status = {
    config: { root: "/synthetic/collection", workspace: "default", client_id: "SYNTHETIC_CLIENT", oauth_port: 8788, icons_scope: false },
    connection: { usable: true, saved: true, expiresAt: null, scopes: [] },
    albums: [syntheticAlbum("Synthetic Album")],
    job: null, scanRevision: 0, lastUpload: null, inspection: null,
    reviewInspection: reviewReport("initial-review", [reviewResult("Synthetic Album")], 0),
  };
  const globals = {
    document: { querySelector: selector => selector === "#app" ? element : null, getElementById: () => null,
      activeElement: null, documentElement: { lang: "" }, title: "" },
    window: { scrollTo() {}, confirm: message => { confirmations.push(message); return confirm; },
      open: (...args) => { opened.push(args); return null; } },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    navigator: { clipboard: { writeText: async text => { copied.push(text); } } },
    setInterval: handler => { poll = handler; return 1; },
    fetch: async (route, options) => {
      let response = { ok: true };
      if (options) {
        assert.equal(options.headers["X-Yoto-CSRF"], "synthetic-csrf");
        const body = JSON.parse(options.body);
        writes.push({ route, body });
        if (route === "/api/config") {
          status.config = body;
          if (holdNextConfig) {
            holdNextConfig = false;
            await new Promise(resolve => { releaseConfig = resolve; });
            releaseConfig = null;
          }
        }
        if (route === "/api/jobs") {
          status.job = { id: `synthetic-job-${++jobNumber}`, operation: body.operation, state: "running", total: 1, completed: [],
            phase: body.operation, logs: [], skipped: [], currentAlbum: "", audioDone: 0, audioTotal: 0, error: null, loginUrl: null };
          const startedJob = status.job;
          if (body.operation === "scan") {
            status.albums = []; status.reviewInspection = null; status.inspection = null;
          }
          if (body.operation === "inspect") {
            const inspection = { id: status.job.id, operation: "inspect", state: "running", requested: body.albums,
              completed: [], skipped: [], results: [], error: null, sourceUploadId: status.lastUpload?.id ?? null,
              scanRevision: status.scanRevision };
            if (body.purpose === "review") status.reviewInspection = inspection;
            else status.inspection = inspection;
          }
          if (body.operation === "upload" || body.operation === "recreate") {
            status.lastUpload = { id: status.job.id, operation: body.operation, state: "running",
              requested: body.operation === "upload" ? body.albums : [body.album],
              completed: [], skipped: [], reviewSkipped: body.skippedTitles, error: null };
            status.reviewInspection = null; status.inspection = null;
          }
          response = { job: startedJob };
          if (nextJobStatus === "missing") status.job = null;
          if (nextJobStatus === "replacement") {
            status.job = { ...startedJob, id: "another-window-job", operation: "upload", phase: "upload" };
          }
          nextJobStatus = "started";
        }
      }
      const body = route === "/api/bootstrap" ? { csrf: "synthetic-csrf" } : route === "/api/status" ? status :
        route === "/api/asset" ? { chosen } : response;
      const snapshot = structuredClone(body);
      if (route === "/api/status" && holdNextStatus) {
        holdNextStatus = false;
        await new Promise(resolve => { releaseStatus = resolve; });
        releaseStatus = null;
      }
      return { ok: true, json: async () => snapshot };
    },
  };
  for (const [key, value] of Object.entries(globals)) {
    original[key] = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { value, writable: true, configurable: true });
  }
  try {
    let source = await readFile(new URL("../web/app.js", import.meta.url), "utf8");
    source = source.replace('"./i18n.mjs"', JSON.stringify(new URL("../web/i18n.mjs", import.meta.url).href));
    await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
    for (let i = 0; i < 10 && !poll; i++) await new Promise(resolve => setImmediate(resolve));
    const completeReview = async results => {
      assert.equal(status.job.operation, "inspect");
      assert.equal(status.reviewInspection?.id, status.job.id);
      status.reviewInspection = reviewReport(status.job.id, results, status.scanRevision);
      status.job = { ...status.job, state: "completed" };
      await poll();
    };
    const waitFor = async (condition, message) => {
      for (let i = 0; i < 50 && !condition(); i++) await new Promise(resolve => setImmediate(resolve));
      assert.ok(condition(), message);
    };
    assert.ok(element.innerHTML.includes(messages.en.title2));
    assert.ok(element.innerHTML.includes("<span>Yotolio<small>"));
    assert.ok(globals.document.title.startsWith("Yotolio · "));
    const click = dataset => events.click({ target: { closest: () => ({ dataset, disabled: false }) }, preventDefault() {} });
    const input = (name, title, value) => {
      const match = (element.innerHTML.match(/<input\b[^>]*>/g) ?? []).find(item =>
        item.includes(`name="${name}"`) && item.includes(`data-title="${title}"`)
        && (value === undefined || item.includes(`value="${value}"`)));
      assert.ok(match, `Missing ${name} input for ${title}`);
      return match;
    };
    await click({ action: "guide" });
    assert.ok(element.innerHTML.includes("From folder to first listen."));
    assert.ok(element.innerHTML.includes("macOS, open Yotolio.app;"));
    assert.ok(element.innerHTML.includes("Public Client"));
    assert.ok(element.innerHTML.includes("click Continue to scan automatically"));
    events.change({ target: { id: "language", value: "zh" } });
    assert.equal(storage.get("yoto-language"), "zh");
    assert.equal(globals.document.documentElement.lang, "zh-CN");
    assert.ok(element.innerHTML.includes("从一个文件夹，到第一次收听。"));
    assert.ok(element.innerHTML.includes("macOS 打开 Yotolio.app；"));
    assert.ok(element.innerHTML.includes("user:content:manage"));
    await click({ step: "1" });
    assert.ok(!element.innerHTML.includes("My collection/"));
    assert.ok(!element.innerHTML.includes("封面会复制到 Covers"));
    assert.ok(element.innerHTML.includes("点击“继续”后会自动扫描"));
    assert.ok(!element.innerHTML.includes('data-action="scan"'), "The source step has no separate scan button");
    assert.ok(element.innerHTML.includes("不搜索 Yoto 账号"));
    assert.ok(!element.innerHTML.includes('name="upload-mode"'), "There is no separate icon-update mode");

    await click({ action: "next" });
    assert.deepEqual(writes.at(-1).body, { operation: "scan", album: null });
    assert.ok(element.innerHTML.includes("正在扫描素材"));
    assert.ok(element.innerHTML.includes("<progress class=\"indeterminate\""));
    assert.ok(element.innerHTML.includes("选择素材"), "The wizard waits on the source step during scanning");
    status.albums = [syntheticAlbum("Synthetic Album")];
    status.scanRevision++;
    status.job = { ...status.job, state: "completed", phase: "prepared" };
    holdNextConfig = true;
    const preparingReview = poll();
    await waitFor(() => releaseConfig, "Yoto review preparation did not start");
    assert.ok(element.innerHTML.includes(messages.zh.preparingReview));
    assert.ok(element.innerHTML.includes(messages.zh.checkingNote));
    assert.ok(element.innerHTML.includes('/api/cover?index=0&v=1'),
      "Review cover URLs use the scan revision rather than the current job ID");
    releaseConfig();
    await preparingReview;
    assert.deepEqual(writes.at(-1).body, {
      operation: "inspect", album: null, albums: ["Synthetic Album"], purpose: "review",
    }, "Review checks existing Yoto cards automatically");
    assert.ok(element.innerHTML.includes(messages.zh.checkingNote));
    assert.ok(element.innerHTML.includes(translate("zh", "checkedCount", { done: 0, total: 1 })));
    assert.ok(element.innerHTML.includes('<progress max="1" value="0"></progress>'));
    status.job = { ...status.job, completed: ["Synthetic Album"], currentAlbum: "Synthetic Album" };
    await poll();
    assert.ok(element.innerHTML.includes(translate("zh", "lastChecked", { album: "Synthetic Album" })));
    assert.ok(element.innerHTML.includes(translate("zh", "checkedCount", { done: 1, total: 1 })));
    assert.ok(element.innerHTML.includes('<progress max="1" value="1"></progress>'),
      "Review progress must use the number actually inspected");
    await completeReview([reviewResult("Synthetic Album", "present", { remoteTracks: 6, remoteIcons: 0 })]);
    assert.ok(element.innerHTML.includes('/api/cover?index=0&v=1'),
      "Completing the inspection must not change the cover URL");
    assert.ok(element.innerHTML.includes(messages.zh.title2));
    assert.ok(!element.innerHTML.includes("操作已完成"), "Review must not show a scan-completed task panel");
    assert.ok(!element.innerHTML.includes("Activity log"));
    assert.ok(element.innerHTML.includes("音频 6 → 1 · 图标 0 → 1 · 封面 1 → 1"));
    assert.ok(!input("selected-card", "Synthetic Album").includes(" checked"), "Existing cards default to unselected");
    assert.ok(input("existing-action-0", "Synthetic Album", "skip").includes(" checked"));
    events.change({ target: { name: "existing-action-0", value: "replace", dataset: { title: "Synthetic Album" } } });
    assert.ok(input("selected-card", "Synthetic Album").includes(" checked"), "Replace selects the card");
    events.change({ target: { name: "existing-action-0", value: "skip", dataset: { title: "Synthetic Album" } } });
    assert.ok(!input("selected-card", "Synthetic Album").includes(" checked"), "Skip deselects the card");
    events.change({ target: { name: "selected-card", checked: true, dataset: { title: "Synthetic Album" } } });
    assert.ok(input("existing-action-0", "Synthetic Album", "replace").includes(" checked"), "Selecting an existing card selects its action");
    events.change({ target: { name: "selected-card", checked: false, dataset: { title: "Synthetic Album" } } });
    assert.ok(input("existing-action-0", "Synthetic Album", "skip").includes(" checked"), "Deselecting restores Skip");
    await click({ action: "select-all" });
    assert.ok(!input("selected-card", "Synthetic Album").includes(" checked"), "Select all new never authorizes replacement");

    events.change({ target: { name: "existing-action-0", value: "replace", dataset: { title: "Synthetic Album" } } });
    status.reviewInspection = reviewReport("unknown-count-review", [
      reviewResult("Synthetic Album", "present", { remoteTracks: null }),
    ], status.scanRevision);
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.countsUnavailable), "Unknown remote counts must not become zero");
    assert.ok(!input("selected-card", "Synthetic Album").includes(" checked"), "A new review clears replacement authorization");
    assert.ok(input("existing-action-0", "Synthetic Album", "skip").includes(" checked"));

    await click({ action: "clear-selection" });
    assert.ok(element.innerHTML.includes(messages.zh.noSelection));
    assert.match(element.innerHTML, /data-action="upload" disabled/);
    await click({ step: "3" });
    await click({ step: "4" });
    await click({ action: "next" });
    assert.ok(element.innerHTML.includes(messages.zh.title2), "No navigation route may bypass an empty selection");
    await click({ action: "select-all" });
    status.reviewInspection = reviewReport("read-only-review", [
      reviewResult("Synthetic Album", "duplicate"),
    ], status.scanRevision);
    status.lastUpload = { id: "old-batch", operation: "upload", state: "completed",
      requested: ["Old card"], completed: ["Old card"], skipped: [], error: null };
    await poll();
    events.change({ target: { name: "selected-card", checked: true, dataset: { title: "Synthetic Album" } } });
    const beforeReadOnly = writes.length;
    await click({ action: "next" });
    assert.ok(element.innerHTML.includes(messages.zh.readOnlyNotSent));
    assert.ok(!element.innerHTML.includes("Old card"), "All-skipped results must not reuse the previous batch");
    assert.equal(writes.length, beforeReadOnly, "Viewing skipped cards must not start another task");
    status.connection.usable = false;
    status.reviewInspection = null;
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.reviewStale));
    assert.ok(!element.innerHTML.includes("Old card"), "A stale review must not expose the previous batch");
    await click({ action: "back" });
    assert.ok(element.innerHTML.includes(messages.zh.automaticAccounts), "Back returns to connection after disconnect");
    status.connection.usable = true;
    status.albums = [syntheticAlbum("Synthetic Album"), syntheticAlbum("Duplicate")];
    status.reviewInspection = reviewReport("fresh-review", [
      reviewResult("Synthetic Album", "present"), reviewResult("Duplicate", "duplicate"),
    ], status.scanRevision);
    await poll();
    await click({ step: "3" });
    events.change({ target: { name: "existing-action-0", value: "replace", dataset: { title: "Synthetic Album" } } });
    events.change({ target: { name: "selected-card", checked: true, dataset: { title: "Duplicate" } } });
    assert.ok(element.innerHTML.includes(messages.zh.title2));
    assert.ok(input("selected-card", "Synthetic Album").includes(" checked"));
    assert.ok(input("existing-action-0", "Synthetic Album", "replace").includes(" checked"),
      "The review page displays the selected card and its action");
    const notIncluded = translate("zh", "selectedNotIncluded", { count: 1 });
    assert.ok(element.innerHTML.includes(notIncluded), "Selected cards excluded from the task need a clear warning");
    events.change({ target: { name: "selected-card", checked: false, dataset: { title: "Duplicate" } } });
    assert.ok(element.innerHTML.includes("将处理 1 张"));
    assert.ok(!element.innerHTML.includes(notIncluded) && !element.innerHTML.includes("不处理 0 张"),
      "An unselected card must not produce a misleading zero-skipped count");
    events.change({ target: { name: "selected-card", checked: true, dataset: { title: "Duplicate" } } });
    let before = writes.length;
    await click({ action: "upload" });
    assert.equal(writes.length, before, "Cancel must not change config or start an upload");
    confirm = true;
    await click({ action: "upload" });
    assert.deepEqual(writes.at(-1).body, { operation: "upload", album: null, albums: ["Synthetic Album"],
      reviewId: "fresh-review", overwriteTitles: ["Synthetic Album"], skippedTitles: ["Duplicate"], confirmed: true });
    assert.ok(confirmations.at(-1).includes("Synthetic Album") && confirmations.at(-1).includes("确认覆盖"),
      "The confirmation identifies the card and the replacement action");
    assert.ok(input("selected-card", "Synthetic Album").includes(" checked")
      && input("selected-card", "Synthetic Album").includes(" disabled"),
      "Review retains the submitted card while the review is invalidated");
    assert.ok(element.innerHTML.includes(messages.zh.batchIncluded));
    assert.ok(element.innerHTML.includes(notIncluded), "The exclusion warning remains visible after submission");
    const jobsWhileRunning = writes.filter(item => item.route === "/api/jobs").length;
    await click({ step: "4" });
    assert.ok(element.innerHTML.includes(translate("zh", "batchSummary",
      { done: 0, skipped: 0, unfinished: 1, total: 1 })), "Running cards remain unfinished, not skipped");
    assert.ok(element.innerHTML.includes(messages.zh.batchRunning));
    assert.ok(element.innerHTML.includes(notIncluded));
    await click({ step: "3" });
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, jobsWhileRunning,
      "Viewing a running batch must not start another task");
    const batchWithExclusions = status.lastUpload;
    status.lastUpload = { ...batchWithExclusions };
    delete status.lastUpload.reviewSkipped;
    await poll();
    assert.ok(!element.innerHTML.includes(notIncluded), "Older batches without exclusions must not be guessed from selection");
    status.lastUpload = batchWithExclusions;
    status.job = { ...status.job, state: "stopped" };
    status.lastUpload = { ...status.lastUpload, state: "stopped" };
    const beforePaused = writes.filter(item => item.route === "/api/jobs").length;
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.resumeAfterPause));
    await click({ step: "4" });
    assert.ok(element.innerHTML.includes(messages.zh.batchStopped));
    assert.ok(element.innerHTML.includes(translate("zh", "batchSummary",
      { done: 0, skipped: 0, unfinished: 1, total: 1 })), "Paused cards remain visibly unfinished");
    await click({ step: "3" });
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, beforePaused,
      "Viewing a paused batch must not automatically replace it with an online check");
    await click({ action: "recheck-review" });
    assert.equal(writes.at(-1).body.purpose, "review", "Resuming begins with an explicit recheck");
    await completeReview([reviewResult("Synthetic Album", "present"), reviewResult("Duplicate", "duplicate")]);
    assert.ok(!input("selected-card", "Synthetic Album").includes(" checked"),
      "Rechecking must not silently authorize another replacement");
    events.change({ target: { name: "existing-action-0", value: "replace", dataset: { title: "Synthetic Album" } } });
    await click({ action: "upload" });
    status.job = { ...status.job, state: "failed", error: "Synthetic source mismatch" };
    status.lastUpload = { ...status.lastUpload, state: "failed", error: "Synthetic source mismatch" };
    const beforeFailed = writes.filter(item => item.route === "/api/jobs").length;
    await poll();
    assert.ok(element.innerHTML.includes("Synthetic source mismatch"));
    assert.ok(element.innerHTML.includes("有一处需要你检查"));
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, beforeFailed,
      "A failed upload must not automatically start an inspection");
    const failedJobId = status.job.id;
    status.job = { ...status.job, recovery: { type: "missing_prior_card", title: "Synthetic Album",
      priorStatus: "pending_create" } };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.recoveryTitle));
    assert.ok(element.innerHTML.includes(messages.zh.recoveryPending));
    assert.ok(!element.innerHTML.includes("Synthetic source mismatch"), "The recovery explanation is localized");
    before = writes.length;
    await click({ step: "4" });
    assert.ok(element.innerHTML.includes(messages.zh.resolveRecovery));
    assert.match(element.innerHTML, /<button[^>]*data-action="scan"[^>]* disabled/);
    assert.match(element.innerHTML, /<button[^>]*data-action="choose-cover"[^>]* disabled/);
    await click({ action: "choose-cover", album: "Synthetic Album" });
    assert.equal(writes.length, before, "A pending recovery cannot open a picker or trigger an inspection");
    await click({ step: "1" });
    assert.match(element.innerHTML, /data-action="next" disabled/);
    await click({ action: "next" });
    assert.equal(writes.length, before, "A pending recovery cannot start a scan");
    await click({ step: "3" });
    confirm = false;
    before = writes.length;
    await click({ action: "recreate" });
    assert.equal(writes.length, before, "Cancelling recovery cannot save settings or create a card");
    confirm = true;
    await click({ action: "recreate" });
    assert.deepEqual(writes.at(-1).body, { operation: "recreate", album: "Synthetic Album",
      confirmed: true, recoveryJobId: failedJobId });
    status.job = { ...status.job, state: "completed" };
    status.lastUpload = { ...status.lastUpload, state: "completed" };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.recreateContinue));
    const afterRecreation = writes.filter(item => item.route === "/api/jobs").length;
    status.albums = ["First", "Second", "Third", "Interrupted"].map(title => ({
      title, tracks: 1, icons: 0, cover: null, coverStatus: "missing", audioIssue: null,
      trackDetails: [{ name: "One.mp3", hasIcon: false, issue: "missing" }], unmatchedIcons: [],
    }));
    await poll();
    await click({ step: "3" });
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, afterRecreation,
      "A completed one-card recreation must wait for an explicit recheck");
    await click({ action: "recheck-review" });
    await completeReview(status.albums.map(album => reviewResult(album.title)));
    for (const title of ["First", "Second", "Third", "Interrupted"]) assert.ok(element.innerHTML.includes(title));
    assert.ok(element.innerHTML.includes("/myo-placeholder.svg"));
    events.change({ target: { name: "selected-card", checked: false, dataset: { title: "First" } } });
    await click({ action: "select-all" });
    assert.ok(input("selected-card", "First").includes(" checked"), "Select all new selects missing cards");
    events.change({ target: { name: "selected-card", checked: false, dataset: { title: "Second" } } });
    status.config.workspace = "account-b";
    status.albums = [];
    status.reviewInspection = null;
    status.job = { ...status.job, id: "switch-account", operation: "auth", state: "running", phase: "account-scan" };
    await poll();
    status.albums = ["First", "Second", "Third", "Interrupted"].map(title => ({
      title, tracks: 1, icons: 0, cover: null, coverStatus: "missing", audioIssue: null,
      trackDetails: [{ name: "One.mp3", hasIcon: false, issue: "missing" }], unmatchedIcons: [],
    }));
    status.job = { ...status.job, state: "completed", phase: "connected" };
    await poll();
    await completeReview([
      reviewResult("First"), reviewResult("Second"),
      reviewResult("Third", "present", { pendingOverwrite: true }),
      reviewResult("Interrupted", "present", { pendingCreate: true }),
    ]);
    assert.ok(element.innerHTML.includes(messages.zh.resumeOverwrite));
    assert.ok(element.innerHTML.includes(messages.zh.resumeCreation));
    events.change({ target: { name: "existing-action-2", value: "replace", dataset: { title: "Third" } } });
    events.change({ target: { name: "existing-action-3", value: "replace", dataset: { title: "Interrupted" } } });
    const accountReviewID = status.reviewInspection.id;
    await click({ action: "upload" });
    assert.deepEqual(writes.at(-1).body, { operation: "upload", album: null, albums: ["First", "Third", "Interrupted"],
      reviewId: accountReviewID,
      overwriteTitles: ["Third", "Interrupted"], skippedTitles: [], confirmed: true });
    assert.ok(confirmations.at(-1).includes("不会再次发送覆盖"), "An interrupted replacement is only checked");
    assert.ok(confirmations.at(-1).includes("这些卡片不会被覆盖"), "An interrupted creation is not replaced");
    const uncertainBatch = status.job.id;
    const skippedCard = { title: "Third", reason: "already_existing" };
    status.job = { ...status.job, state: "failed", error: "Synthetic technical detail",
      errorCode: "OVERWRITE_UNCONFIRMED", completed: ["First"], skipped: [skippedCard] };
    status.lastUpload = { ...status.lastUpload, state: "failed", error: "Synthetic technical detail",
      errorCode: "OVERWRITE_UNCONFIRMED", completed: ["First"], skipped: [skippedCard] };
    status.inspection = { id: "batch-check", operation: "inspect", state: "completed",
      requested: ["First", "Third", "Interrupted"], completed: [], skipped: [], results: [],
      error: null, sourceUploadId: uncertainBatch };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.OVERWRITE_UNCONFIRMED));
    await click({ step: "4" });
    assert.ok(element.innerHTML.includes(messages.zh.OVERWRITE_UNCONFIRMED));
    assert.ok(element.innerHTML.includes(translate("zh", "batchSummary",
      { done: 1, skipped: 1, unfinished: 1, total: 3 })),
      "A failed batch distinguishes processed, skipped and unfinished submitted cards");
    assert.ok(!element.innerHTML.includes("Synthetic technical detail"));
    status.job.errorCode = "OVERWRITE_CHANGED";
    status.lastUpload.errorCode = "OVERWRITE_CHANGED";
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.OVERWRITE_CHANGED));
    assert.ok(!element.innerHTML.includes("Synthetic technical detail"));
    await click({ step: "3" });
    status.albums = [{ title: "Another Collection", tracks: 1, icons: 1, cover: "cover.png",
      coverStatus: "provided", audioIssue: null, trackDetails: [{ name: "One.mp3", hasIcon: true, issue: null }] }];
    status.scanRevision++;
    status.job = { ...status.job, id: "new-scan", operation: "scan", state: "completed", phase: "prepared" };
    status.reviewInspection = reviewReport("new-review", [reviewResult("Another Collection")], status.scanRevision);
    await poll();
    assert.ok(element.innerHTML.includes('/api/cover?index=0&v=2'),
      "A later scan must change the cover URL");
    nextJobStatus = "missing";
    const jobsBeforeUnknownUpload = writes.filter(item => item.route === "/api/jobs").length;
    await click({ action: "upload" });
    assert.ok(element.innerHTML.includes(messages.zh.taskStatusTitle));
    status.job = { id: "reconciled-scan", operation: "scan", state: "completed", phase: "prepared",
      total: 1, completed: [], skipped: [], currentAlbum: "", audioDone: 0, audioTotal: 0, logs: [] };
    status.reviewInspection = reviewReport("new-review", [reviewResult("Another Collection")], status.scanRevision);
    await poll();
    await click({ action: "upload" });
    await click({ action: "recheck-review" });
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, jobsBeforeUnknownUpload + 1,
      "An unknown upload outcome blocks both another upload and an automatic or manual check");
    await click({ action: "dismiss-task-warning" });
    await click({ action: "upload" });
    assert.deepEqual(writes.at(-1).body, { operation: "upload", album: null, albums: ["Another Collection"],
      reviewId: "new-review", overwriteTitles: [], skippedTitles: [], confirmed: true },
      "A changed collection must not reuse a stale selection");
    status.job = { ...status.job, state: "completed", completed: ["Another Collection"] };
    status.lastUpload = { ...status.lastUpload, state: "completed", completed: ["Another Collection"] };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.title5), "A successful upload opens card results");
    assert.deepEqual(writes.at(-1).body, { operation: "inspect", album: null, albums: ["Another Collection"] },
      "The successful batch starts a read-only check of its submitted cards");
    status.job = { ...status.job, state: "completed" };
    status.inspection = { ...status.inspection, state: "completed", completed: ["Another Collection"] };
    await poll();
    await click({ step: "3" });
    await completeReview([reviewResult("Another Collection")]);
    await click({ action: "upload" });
    status.job = { ...status.job, state: "failed" };
    status.lastUpload = { ...status.lastUpload, state: "failed" };
    status.inspection = { id: "new-batch-check", operation: "inspect", state: "completed",
      requested: ["Another Collection"], completed: [], skipped: [], results: [], error: null,
      sourceUploadId: status.lastUpload.id };
    status.albums[0] = { ...status.albums[0], cover: null, coverStatus: "missing", icons: 0,
      unmatchedIcons: ["/synthetic/unmatched.png"],
      trackDetails: [{ name: '<script>song</script>.mp3', hasIcon: false, issue: "ambiguous",
        candidates: ["/synthetic/<unsafe>.png", "/synthetic/other.png"] }] };
    await poll();
    await click({ step: "4" });
    assert.ok(element.innerHTML.includes(messages.zh.title5));
    assert.ok(element.innerHTML.includes("没有找到封面"));
    assert.ok(element.innerHTML.includes("1 首歌曲需要图标"));
    assert.ok(element.innerHTML.includes("unmatched.png"));
    assert.ok(element.innerHTML.includes("&lt;unsafe&gt;.png"));
    assert.ok(element.innerHTML.includes("&lt;script&gt;song&lt;/script&gt;.mp3"));
    assert.ok(!element.innerHTML.includes("<script>song</script>.mp3"));
    before = writes.length;
    await click({ action: "choose-icon", album: "Another Collection", track: "0" });
    assert.equal(writes.length, before + 1, "Cancelling the picker must not trigger a scan");
    chosen = true;
    const updatedAlbum = structuredClone(status.albums[0]);
    await click({ action: "choose-cover", album: "Another Collection" });
    assert.deepEqual(writes.at(-2).body, { album: "Another Collection", kind: "cover", track: null });
    assert.deepEqual(writes.at(-1).body, { operation: "scan", album: null });
    status.assetStatusError = '<script>invalid evidence</script>';
    status.albums = [{ ...updatedAlbum, pendingCoverUpdate: true }];
    status.scanRevision++;
    status.job = { ...status.job, state: "completed", phase: "prepared" };
    await poll();
    assert.ok(element.innerHTML.includes("&lt;script&gt;invalid evidence&lt;/script&gt;"));
    assert.ok(!element.innerHTML.includes("<script>invalid evidence</script>"));
    assert.ok(element.innerHTML.includes(messages.zh.updateInterrupted));
    status.inspection = { ...status.inspection, state: "completed" };
    status.job = { ...status.job, state: "completed" };
    await poll();

    await click({ step: "2" });
    status.connection.usable = true;
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.automaticAccounts));
    assert.match(element.innerHTML, /<details class="optional-setting" data-detail="accountOptions">/,
      "Account management is collapsed by default");
    assert.ok(!element.innerHTML.includes("Private workspace name"));
    status.connection.saved = false;
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.differentAccount));
    await click({ action: "switch-account" });
    assert.deepEqual(writes.at(-1).body, { operation: "auth", album: null, switchAccount: true });
    status.job = { ...status.job, state: "completed", phase: "connected" };
    status.connection.saved = true;
    await poll();
    await completeReview([reviewResult("Another Collection")]);
    await click({ step: "2" });
    await click({ action: "setup-help" });
    for (const name of ["create", "callback", "scopes"]) {
      assert.ok(element.innerHTML.includes(`src="/setup-${name}.png"`));
    }
    assert.ok(element.innerHTML.includes(messages.zh.setupHelpNote));
    assert.ok(element.innerHTML.includes(messages.zh.setupStep2Text));
    assert.ok(element.innerHTML.includes(messages.zh.exampleOnly));
    assert.ok(element.innerHTML.includes(messages.zh.clientIdExampleHint));
    assert.ok(element.innerHTML.includes("example-client-id"));
    assert.ok(element.innerHTML.includes('class="copy-example"'));
    assert.ok(!element.innerHTML.includes('src="/setup-guide.svg"'));
    events.change({ target: { id: "language", value: "en" } });
    assert.ok(element.innerHTML.includes(escapeHTML(messages.en.setupHelpNote)));
    assert.ok(element.innerHTML.includes(escapeHTML(messages.en.setupStep2Text)));
    assert.ok(!element.innerHTML.includes(messages.zh.setupHelpNote));
    events.change({ target: { id: "language", value: "zh" } });
    events.input({ target: { dataset: { field: "client_id" }, type: "text", value: "OTHER_CLIENT" } });
    assert.ok(element.innerHTML.includes(messages.zh.expired), "An edited client ID must show reconnect is required");
    assert.match(element.innerHTML, /data-action="next" disabled/);
    events.input({ target: { dataset: { field: "client_id" }, type: "text", value: "SYNTHETIC_CLIENT" } });
    assert.ok(element.innerHTML.includes(messages.zh.connected));
    await click({ action: "copy" });
    assert.deepEqual(copied, ["http://127.0.0.1:8788/callback"]);
    assert.ok(element.innerHTML.includes(messages.zh.copied));

    const nativeCalls = [];
    globals.window.webkit = { messageHandlers: { yotolio: {
      postMessage: async message => { nativeCalls.push(message); return true; },
    } } };
    await click({ action: "copy" });
    assert.deepEqual(nativeCalls.at(-1), { type: "copy", value: "http://127.0.0.1:8788/callback" });
    assert.equal(copied.length, 1, "The native window uses its own clipboard bridge");
    globals.window.webkit.messageHandlers.yotolio.postMessage = async () => { throw new Error("Synthetic clipboard failure"); };
    await click({ action: "copy" });
    assert.ok(element.innerHTML.includes("Synthetic clipboard failure"));
    globals.window.webkit.messageHandlers.yotolio.postMessage = async message => { nativeCalls.push(message); return true; };

    await click({ action: "auth" });
    assert.equal(status.job.operation, "auth");
    assert.deepEqual(opened, [], "Login waits for the callback server");
    status.job = { ...status.job, loginUrl: "http://127.0.0.1:8788/start" };
    await poll();
    assert.equal(opened.length, 1);
    assert.equal(opened[0][0], "http://127.0.0.1:8788/start");
    assert.ok(element.innerHTML.includes("打开 Yoto 登录页"), "A manual link remains if the popup is blocked");
    await poll();
    assert.equal(opened.length, 1, "Polling must not repeatedly open login");
    status.job = { ...status.job, state: "completed", phase: "connected" };
    status.reviewInspection = null;
    await poll();
    await completeReview([reviewResult("Another Collection")]);
    await click({ step: "2" });
    await click({ action: "auth" });
    status.job = { ...status.job, state: "stopping", loginUrl: "http://127.0.0.1:8788/start" };
    await poll();
    assert.equal(opened.length, 1, "Cancelling connection must not open login");
    assert.ok(!element.innerHTML.includes("打开 Yoto 登录页"));
    status.job = { ...status.job, state: "stopped" };
    await poll();
    assert.ok(!element.innerHTML.includes(messages.zh.phase_auth), "A cancelled login is no longer waiting");
    await click({ action: "auth" });
    status.job = { ...status.job, loginUrl: "https://example.invalid/start" };
    await poll();
    assert.equal(opened.length, 1, "An unsafe login URL must not be opened");
    assert.ok(element.innerHTML.includes(messages.zh.invalidLoginLink));
    status.job = { ...status.job, state: "failed", error: "ACCOUNT_IDENTITY_UNAVAILABLE",
      errorCode: "ACCOUNT_IDENTITY_UNAVAILABLE" };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.ACCOUNT_IDENTITY_UNAVAILABLE));
    const loginFailures = ["ACCOUNT_IDENTITY_REJECTED", "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE",
      "ACCOUNT_IDENTITY_KEYS_UNAVAILABLE", "ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE",
      "ACCOUNT_IDENTITY_TOKEN_INVALID", "ACCOUNT_AUTH_RESPONSE_INVALID", "ACCOUNT_AUTH_TOKEN_INVALID",
      "ACCOUNT_AUTH_EXPIRY_INVALID", "ACCOUNT_AUTH_SCOPES_INSUFFICIENT",
      "ACCOUNT_AUTH_CONTENT_VIEW_DENIED", "ACCOUNT_AUTH_ICON_ACCESS_DENIED",
      "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE"];
    for (const language of ["en", "zh"]) {
      events.change({ target: { id: "language", value: language } });
      for (const code of loginFailures) {
        status.job = { ...status.job, phase: "auth", error: "Synthetic technical detail", errorCode: code };
        await poll();
        const html = element.innerHTML;
        assert.ok(html.includes(escapeHTML(messages[language][code])), `${code} must be localized`);
        assert.ok(!html.includes("Synthetic technical detail"), "Known login failures do not expose raw details");
        assert.ok(!html.includes(messages[language].phase_auth), "A failed login is no longer waiting");
        assert.ok(html.indexOf('data-action="auth"') < html.indexOf('class="panel job failed"'));
        assert.ok(html.indexOf('class="panel job failed"') < html.indexOf('data-detail="accountOptions"'),
          "The failure is beside the connection action, ahead of account management");
      }
    }
    status.connection.error = "<synthetic saved-login error>";
    await poll();
    assert.ok(element.innerHTML.includes("&lt;synthetic saved-login error&gt;"));
    assert.ok(!element.innerHTML.includes("<synthetic saved-login error>"));
    delete status.connection.error;

    status.connection.usable = false;
    status.connection.saved = false;
    holdNextConfig = true;
    const connecting = click({ action: "auth" });
    await waitFor(() => releaseConfig, "Connection preparation did not start");
    assert.ok(element.innerHTML.includes(messages.zh.preparingConnection));
    assert.ok(element.innerHTML.includes(messages.zh.connectingStatus));
    assert.match(element.innerHTML, /data-action="auth" disabled/);
    releaseConfig();
    await connecting;
    const confirmationJobID = status.job.id;
    status.accountMigration = { id: "synthetic-choice", name: "<earlier profile>", remaining: 1 };
    status.job = { ...status.job, state: "running", phase: "account-confirmation", error: null, errorCode: null };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.accountQuestion));
    assert.ok(element.innerHTML.includes("&lt;earlier profile&gt;"));
    assert.ok(element.innerHTML.includes(messages.zh.confirmationNeeded));
    assert.match(element.innerHTML, /data-action="account-different" disabled/,
      "The question waits for the login worker to finish");
    status.job = { ...status.job, state: "completed" };
    await poll();
    assert.doesNotMatch(element.innerHTML, /data-action="account-different" disabled/);
    assert.ok(element.innerHTML.indexOf('class="persistent-alerts"')
      < element.innerHTML.indexOf('class="account-confirmation"'));
    await click({ action: "back" });
    assert.ok(element.innerHTML.includes(messages.zh.accountQuestion), "The question remains visible on the source step");
    assert.ok(element.innerHTML.includes("点击“继续”后会自动扫描"));
    assert.match(element.innerHTML, /data-action="next" disabled/);
    const beforeBlockedContinue = writes.length;
    await click({ action: "next" });
    assert.equal(writes.length, beforeBlockedContinue, "Confirmation cannot be bypassed from the source step");
    await click({ action: "guide" });
    assert.ok(element.innerHTML.includes(messages.zh.accountQuestion), "The question remains visible in the guide");
    await click({ step: "2" });
    await click({ step: "3" });
    assert.ok(element.innerHTML.includes(messages.zh.confirmationNeeded), "Review cannot bypass confirmation");
    await click({ action: "account-different" });
    assert.deepEqual(writes.at(-1), {
      route: "/api/account-migration", body: { id: "synthetic-choice", choice: "different" },
    });
    status.accountMigration = null;
    status.connection.saved = true;
    status.connection.usable = true;
    status.config.workspace = "separate";
    status.albums = [];
    status.reviewInspection = null;
    status.job = { ...status.job, state: "running", phase: "account-scan" };
    await poll();
    await click({ step: "3" });
    assert.ok(element.innerHTML.includes(messages.zh["phase_account-scan"]), "Review waits for the account-specific scan");
    status.albums = [syntheticAlbum("Another Collection")];
    status.scanRevision++;
    status.job = { ...status.job, id: confirmationJobID, state: "completed", phase: "connected" };
    await poll();
    await completeReview([reviewResult("Another Collection")]);
    assert.ok(element.innerHTML.includes("Another Collection"), "Finishing the connection opens Review");

    await click({ step: "2" });
    holdNextStatus = true;
    const stale = poll();
    await waitFor(() => releaseStatus, "The old status response was not held");
    await click({ action: "auth" });
    assert.equal(status.job.operation, "auth");
    releaseStatus();
    await stale;
    assert.ok(!element.innerHTML.includes(messages.zh.taskStatusTitle),
      "A delayed status from before startup must not mark the new task as lost");
    status.job = { ...status.job, state: "completed", phase: "connected" };
    status.reviewInspection = null;
    await poll();
    await completeReview([reviewResult("Another Collection")]);

    await click({ step: "2" });
    status.reviewInspection = null;
    nextJobStatus = "missing";
    const jobsBeforeMissing = writes.filter(item => item.route === "/api/jobs").length;
    await click({ action: "auth" });
    assert.ok(element.innerHTML.includes(messages.zh.taskStatusTitle));
    await poll();
    await click({ step: "3" });
    assert.equal(writes.filter(item => item.route === "/api/jobs").length, jobsBeforeMissing + 1,
      "An unknown task outcome must not trigger an automatic retry or review check");
    await click({ step: "2" });
    await click({ action: "dismiss-task-warning" });
    nextJobStatus = "replacement";
    await click({ action: "auth" });
    assert.ok(element.innerHTML.includes(messages.zh.taskStatusTitle));
    assert.match(element.innerHTML, /data-action="auth" disabled/,
      "A different running task must keep changes disabled");
    status.job = { ...status.job, state: "completed" };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.taskStatusTitle), "The unknown outcome warning persists");
    await click({ action: "dismiss-task-warning" });

    await click({ step: "1" });
    await click({ action: "next" });
    status.albums = [syntheticAlbum("Partial")];
    status.job = { ...status.job, state: "failed", error: "Synthetic scan failure" };
    await poll();
    assert.ok(element.innerHTML.includes("Synthetic scan failure"));
    await click({ step: "2" });
    assert.ok(element.innerHTML.includes("点击“继续”后会自动扫描"), "Failed scans cannot open review");
    await click({ action: "next" });
    status.albums = [syntheticAlbum("Partial")];
    status.job = { ...status.job, state: "stopped" };
    await poll();
    await click({ step: "2" });
    assert.ok(element.innerHTML.includes("点击“继续”后会自动扫描"), "Cancelled scans cannot open review");
    await click({ action: "next" });
    status.job = { ...status.job, state: "completed", phase: "prepared" };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.scanEmpty), "An empty scan must explain the failure");
    await click({ action: "next" });
    status.albums = [syntheticAlbum("Recovered")];
    status.scanRevision++;
    status.job = { ...status.job, state: "completed", phase: "prepared" };
    await poll();
    await completeReview([reviewResult("Recovered")]);
    assert.ok(element.innerHTML.includes("Recovered"), "A successful retry opens review");

    status.albums = ["Recovered", "Other selected", "Not in batch"].map(syntheticAlbum);
    status.scanRevision++;
    status.reviewInspection = reviewReport("batch-review", status.albums.map(album => reviewResult(album.title)),
      status.scanRevision);
    status.lastUpload = {
      id: "batch-1", operation: "upload", state: "completed",
      requested: ["Recovered", "Other selected", "<missing source>"],
      completed: ["Recovered"], skipped: [{ title: "Other selected", reason: "already_existing" }], error: null,
    };
    status.inspection = null;
    await poll();
    const inspectionWrites = () => writes.filter(item => item.route === "/api/jobs" && item.body.operation === "inspect");
    const inspectionBaseline = inspectionWrites().length;
    await click({ step: "4" });
    assert.deepEqual(inspectionWrites().at(-1).body,
      { operation: "inspect", album: null, albums: ["Recovered", "Other selected"] },
      "Automatic inspection must be limited to available cards in the latest batch");
    assert.ok(element.innerHTML.includes("&lt;missing source&gt;"));
    assert.ok(!element.innerHTML.includes("<missing source>"));
    await poll();
    assert.equal(inspectionWrites().length, inspectionBaseline + 1, "Polling must not duplicate an active inspection");

    status.job = { ...status.job, state: "failed" };
    status.inspection = { ...status.inspection, state: "failed", error: "<unsafe check error>" };
    await poll();
    assert.equal(inspectionWrites().length, inspectionBaseline + 1, "A failed inspection must not retry automatically");
    assert.ok(element.innerHTML.includes("&lt;unsafe check error&gt;"));
    assert.ok(!element.innerHTML.includes("<unsafe check error>"));
    await click({ action: "inspect" });
    assert.equal(inspectionWrites().length, inspectionBaseline + 2, "The user can retry a failed inspection");

    status.job = { ...status.job, state: "completed" };
    status.inspection = { ...status.inspection, state: "completed", completed: ["Recovered", "Other selected"],
      results: [
        { title: "Recovered", presence: "present", cover: "matched", error: null,
          tracks: [{ name: "One.mp3", remoteTitle: "<remote song>", audio: "matched", icon: "missing" }] },
        { title: "Other selected", presence: "missing", cover: "unverified", error: null, tracks: [] },
      ] };
    await poll();
    assert.ok(element.innerHTML.includes(messages.zh.presence_present));
    assert.ok(element.innerHTML.includes(messages.zh.presence_missing));
    assert.ok(element.innerHTML.includes(messages.zh.audio_matched));
    assert.ok(element.innerHTML.includes(messages.zh.icon_missing));
    assert.ok(element.innerHTML.includes("&lt;remote song&gt;"));
    assert.ok(!element.innerHTML.includes("<remote song>"));
    await click({ step: "3" });
    await click({ step: "4" });
    assert.equal(inspectionWrites().length, inspectionBaseline + 2, "Returning to results must reuse the current inspection");
    status.scanRevision++;
    status.inspection = null;
    status.job = { ...status.job, id: "rescan", operation: "scan", state: "completed" };
    await poll();
    assert.equal(inspectionWrites().length, inspectionBaseline + 3, "A new scan must trigger a fresh batch inspection");
    status.job = { ...status.job, state: "completed" };
    status.inspection = { ...status.inspection, state: "completed" };
    await poll();

    const beforeQuit = writes.length;
    confirm = false;
    await click({ action: "quit" });
    assert.equal(nativeCalls.some(call => call.type === "quit"), false);
    confirm = true;
    await click({ action: "quit" });
    assert.deepEqual(nativeCalls.at(-1), { type: "quit" });
    assert.equal(writes.length, beforeQuit, "The native app owns safe shutdown, not the web page");
    delete globals.window.webkit;
    await click({ action: "quit" });
    assert.equal(writes.at(-1).route, "/api/quit", "Browser mode retains its shutdown flow");
    assert.ok(element.innerHTML.includes(messages.zh.goodbye));
  } finally {
    for (const key of Object.keys(globals)) {
      if (original[key]) Object.defineProperty(globalThis, key, original[key]);
      else delete globalThis[key];
    }
  }
});
