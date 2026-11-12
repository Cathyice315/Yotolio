import { messages, translate, escapeHTML as esc, progress } from "./i18n.mjs";

const app = document.querySelector("#app");
let language = "en";
try { language = localStorage.getItem("yoto-language") === "zh" ? "zh" : "en"; }
catch (error) { console.warn("Language preference cannot be loaded.", error); }
const ui = { language, step: 1, view: "wizard", status: null, draft: null, csrf: "", error: "", notice: "", offline: false,
  picker: false, setupHelp: false, portAdvanced: false, iconsAdvanced: false, accountOptions: false,
  closed: false, pending: false, taskStatusUnknown: false, startingOperation: null,
  awaitingJobID: null, awaitingOperation: null, awaitingAfterStatusSequence: null, reviewAfterScanID: null,
  openLoginAfterStartID: null, reviewAfterLoginID: null, resultsAfterUploadID: null,
  expanded: new Set(), selected: new Set(), overwrites: new Set(), seen: new Set(), selectionCollection: null,
  automaticInspectionKey: null, automaticReviewKey: null, readOnlyReview: null };
const t = (key, values) => translate(ui.language, key, values);
const tx = (key, values) => esc(t(key, values));
const active = () => !!ui.awaitingJobID || ["running", "stopping"].includes(ui.status?.job?.state);
const accountPending = () => !!ui.status?.accountMigration;
const locked = () => active() || ui.pending || ui.offline || accountPending();
const selectedTitles = () => (ui.status?.albums ?? [])
  .filter(album => !album.audioIssue && ui.selected.has(album.title)).map(album => album.title);
const currentReview = () => {
  const report = ui.status?.reviewInspection;
  const titles = (ui.status?.albums ?? []).map(album => album.title);
  return report && report.scanRevision === ui.status.scanRevision && report.requested.length === titles.length
    && titles.every(title => report.requested.includes(title)) ? report : null;
};
const actionableTitles = () => {
  const report = currentReview();
  if (report?.state !== "completed") return [];
  return selectedTitles().filter(title => {
    const result = report.results?.find(item => item.title === title);
    if (!result || result.error != null) return false;
    if (result.presence === "missing") return !result.pendingOverwrite;
    return result.presence === "present" && ui.overwrites.has(title);
  });
};
const selectableNewTitles = () => {
  const review = currentReview();
  if (review?.state !== "completed") return [];
  const newTitles = new Set((review.results ?? []).filter(result =>
    result.presence === "missing" && result.error == null && !result.pendingOverwrite).map(result => result.title));
  return (ui.status?.albums ?? []).filter(album => !album.audioIssue && newTitles.has(album.title)).map(album => album.title);
};
function clearExistingChoices() {
  for (const title of ui.overwrites) ui.selected.delete(title);
  ui.overwrites.clear();
}
function setSelected(title, selected) {
  if (locked() || currentReview()?.state !== "completed"
      || !ui.status.albums.some(album => album.title === title && !album.audioIssue)) return;
  if (selected) {
    ui.selected.add(title);
    const result = currentReview().results?.find(item => item.title === title);
    if (result?.presence === "present" && result.error == null) ui.overwrites.add(title);
  } else {
    ui.selected.delete(title);
    ui.overwrites.delete(title);
  }
}
const hasCurrentScan = () => {
  const { status, draft } = ui;
  if (!status || !draft || !status.albums.length || !draft.root.trim()
      || draft.root !== status.config.root || draft.workspace !== status.config.workspace) return false;
  if (ui.awaitingJobID && ["scan", "prepare"].includes(ui.awaitingOperation)) return false;
  if (status.job?.operation === "auth" && status.job?.phase === "account-scan" && status.job?.state !== "completed") return false;
  return !(["scan", "prepare"].includes(status.job?.operation) && status.job?.state !== "completed");
};
const hasCurrentConnection = () => !!ui.status?.connection.usable && !accountPending()
  && ui.draft.client_id.trim() === ui.status.config.client_id
  && ui.draft.icons_scope === ui.status.config.icons_scope;
const canNavigate = number => {
  if (!Number.isInteger(number) || number < 1 || number > 4) return false;
  if (accountPending()) return number === 1 || number === 2;
  if (number === 1) return true;
  if (!hasCurrentScan()) return false;
  if (number === 2) return true;
  if (!hasCurrentConnection()) return false;
  if (number === 3) return true;
  return !!ui.readOnlyReview || !!ui.status.lastUpload;
};
const canContinue = () => {
  if (ui.pending || ui.offline || !ui.status || accountPending()) return false;
  if (ui.step === 1) return !active() && !recoveryNeedsAttention() && !!ui.draft.root.trim();
  if (ui.step === 2) return !active() && canNavigate(3);
  if (ui.step === 3) return !active() && !ui.taskStatusUnknown && hasCurrentScan() && hasCurrentConnection()
    && selectedTitles().length > 0 && currentReview()?.state === "completed" && !actionableTitles().length;
  return false;
};
const canUpload = () => !locked() && !ui.taskStatusUnknown && hasCurrentScan()
  && hasCurrentConnection() && actionableTitles().length > 0;
const canResolveRecovery = () => !locked() && !ui.taskStatusUnknown && hasCurrentScan() && hasCurrentConnection();
const presentedUpload = () => {
  const { job, lastUpload } = ui.status ?? {};
  return !currentReview() && job?.id === lastUpload?.id
    && ["upload", "existing", "recreate"].includes(job?.operation) ? lastUpload : null;
};
const waitingAfterUpload = () => {
  const job = ui.status?.job;
  return !!job && !["running", "stopping"].includes(job.state)
    && ((["upload", "existing"].includes(job.operation) && ["failed", "stopped"].includes(job.state))
      || job.operation === "recreate");
};
const preparingReview = () => (ui.startingOperation === "inspect" || ui.awaitingOperation === "inspect")
  && !(ui.status?.job?.operation === "inspect" && ["running", "stopping"].includes(ui.status.job.state)
    && ui.status.job.id === ui.status.reviewInspection?.id);
const hasRecoverableCard = title => (ui.status?.albums ?? []).some(album => album.title === title && !album.audioIssue);
const overviewUpload = () => ui.readOnlyReview ? null : ui.status?.lastUpload;
const overviewTitles = () => {
  if (ui.readOnlyReview) return ui.readOnlyReview.titles;
  const upload = overviewUpload();
  return upload ? [...upload.requested, ...(upload.reviewSkipped ?? [])] : selectedTitles();
};
const inspectableTitles = () => {
  const available = new Set((ui.status?.albums ?? []).map(album => album.title));
  return overviewTitles().filter(title => available.has(title));
};
const recoveryNeedsAttention = () => ui.status?.job?.operation === "upload"
  && ui.status.job.state === "failed" && ui.status.job.recovery?.type === "missing_prior_card";
const currentInspection = () => {
  if (ui.readOnlyReview) {
    const review = currentReview();
    return review?.id === ui.readOnlyReview.id && review.state === "completed"
      && hasCurrentScan() && hasCurrentConnection() ? review : null;
  }
  const report = ui.status?.inspection;
  const titles = inspectableTitles();
  return report && report.requested.length === titles.length
    && titles.every(title => report.requested.includes(title))
    && (report.sourceUploadId ?? null) === (overviewUpload()?.id ?? null) ? report : null;
};
const disabled = value => value ? " disabled" : "";
const svg = name => {
  const shapes = {
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3z"/>',
    shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
    book: '<path d="M12 5c-3-3-8-2-9-1v15c3-2 6-1 9 1 3-2 6-3 9-1V4c-3-2-6-1-9 1zm0 0v15"/>',
    sound: '<path d="M4 10v4m5-8v12m5-15v18m5-14v10"/>',
    copy: '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name]}</svg>`;
};
const external = (url, label, cls = "text-link") =>
  `<a class="${cls}" href="${url}" target="_blank" rel="noopener noreferrer">${tx(label)} ↗</a>`;
const button = (action, label, cls = "button primary", unavailable = false) =>
  `<button type="button" class="${cls}" data-action="${action}"${disabled(unavailable)}>${tx(label)}</button>`;

function validatedLoginURL(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "http:" && url.hostname === "127.0.0.1"
        && Number(url.port) === Number(ui.status?.config.oauth_port) && url.pathname === "/start"
        && !url.username && !url.password && !url.search && !url.hash) return url.href;
    return null;
  } catch (error) {
    if (error instanceof TypeError) return null;
    throw error;
  }
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Yoto-CSRF": ui.csrf },
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(messages[ui.language][value.code] ? t(value.code) : value.error);
    error.code = value.code;
    throw error;
  }
  return value;
}

function metrics(albums = ui.status.albums) {
  return `<div class="metrics">${[
    [albums.length, "cards"], [albums.reduce((n, a) => n + a.tracks, 0), "tracks"],
    [albums.reduce((n, a) => n + a.icons, 0), "icons"],
    [albums.filter(a => a.coverStatus === "provided").length, "covers"],
  ].map(([value, key]) => `<div><strong>${value}</strong><span>${tx(key)}</span></div>`).join("")}</div>`;
}

function intro(number) {
  const copy = [1, 3, 2, 5][number - 1];
  return `<div class="eyebrow">${tx("step", { n: number })}</div><p class="kicker">${tx(`intro${copy}`)}</p>
    <h1>${tx(`title${copy}`)}</h1><p class="lead">${tx(`desc${copy}`)}</p>`;
}

function footer() {
  const noAction = ui.step === 3 && !active() && currentReview()?.state === "completed"
    && !actionableTitles().length;
  return `${noAction ? `<p class="selection-hint">${tx(selectedTitles().length ? "noAction" : "noSelection")}</p>` : ""}
    <div class="step-footer">${ui.step > 1 ? button("back", "back", "button quiet") : "<span></span>"}
    ${ui.step === 3 ? reviewPrimaryAction()
      : ui.step < 3 ? `<button class="button primary" data-action="next"${disabled(!canContinue())}>${tx("next")}${svg("arrow")}</button>` : ""}</div>`;
}

function reviewPrimaryAction() {
  return selectedTitles().length && currentReview()?.state === "completed" && !actionableTitles().length
    ? button("next", "viewWithoutUpload", "button primary", !canContinue())
    : button("upload", "startUpload", "button primary", !canUpload());
}

function connectionBadge() {
  const conn = ui.status.connection;
  const connecting = ui.startingOperation === "auth" || ui.awaitingOperation === "auth"
    || (ui.status.job?.operation === "auth" && ["running", "stopping"].includes(ui.status.job.state));
  const current = hasCurrentConnection() && !connecting;
  return `<span class="pill ${current ? "success" : "neutral"}"><span class="dot"></span>
    ${tx(accountPending() ? "confirmationNeeded" : connecting ? "connectingStatus"
      : current ? "connected" : conn.saved ? "expired" : "notConnected")}</span>`;
}

function accountConfirmation() {
  const migration = ui.status.accountMigration;
  if (!migration) return "";
  const unavailable = active() || ui.pending || ui.offline;
  return `<section class="account-confirmation" aria-live="polite">
    <h2>${tx("accountQuestion")}</h2><p>${tx("accountQuestionDetail", { name: migration.name })}</p>
    <p>${tx("accountKeepSeparate")}</p>
    ${migration.remaining > 1 ? `<p>${tx("accountRemaining", { count: migration.remaining })}</p>` : ""}
    ${ui.offline || ui.error ? `<p class="confirmation-error" role="alert">${ui.offline ? tx("serverOffline") : esc(ui.error)}</p>
      ${ui.offline ? button("refresh", "retry", "button quiet small", ui.pending) : ""}` : ""}
    <div class="actions">${button("account-different", "accountDifferent", "button primary", unavailable)}
      ${button("account-same", "accountRestore", "button secondary", unavailable)}
      ${button("account-cancel", "accountCancel", "button quiet", unavailable)}</div>
    </section>`;
}

function sourceView() {
  return `${intro(1)}
    <section class="panel source-panel"><div class="folder-art">${svg("folder")}<span>♫</span></div>
      <label for="root">${tx("folderLabel")}</label>
      <div class="input-action"><input id="root" data-field="root" value="${esc(ui.draft.root)}" placeholder="${tx("folderPlaceholder")}"${disabled(locked())}>
      ${button("browse", ui.picker ? "browseBusy" : "browse", "button secondary", locked() || ui.picker)}</div>
      <p class="hint">${tx("folderHint")}</p>
      <p class="hint">${tx("autoScanHint")}</p>
      ${recoveryNeedsAttention() ? `<p class="asset-message">${tx("resolveRecovery")}</p>` : ""}
    </section>
    ${jobView(["scan", "prepare"])}${footer()}`;
}

function assetState(album) {
  if (album.audioIssue) return "blocked";
  if (album.coverStatus !== "provided" || album.icons < album.tracks || album.unmatchedIcons?.length) return "needsAssets";
  return "ready";
}

function stateBadge(album) {
  const state = assetState(album);
  return `<span class="ready ${state === "ready" ? "" : "attention"}">${state === "ready" ? svg("check") : '<span class="dot"></span>'}${tx(state)}</span>`;
}

function countComparison(album, result) {
  const counts = result.presence === "missing" ? [0, 0, 0]
    : [result.remoteTracks, result.remoteIcons, result.remoteCovers];
  if (!counts.every(count => Number.isInteger(count) && count >= 0)) {
    return `<p class="review-warning">${tx("countsUnavailable")}</p>`;
  }
  const local = [album.tracks, album.icons, album.coverStatus === "provided" ? 1 : 0];
  return `<div class="count-comparison"><span>${tx("countDirection")}</span><p>${tx("countArrows", {
    audio: `${counts[0]} → ${local[0]}`, icons: `${counts[1]} → ${local[1]}`, covers: `${counts[2]} → ${local[2]}`,
  })}</p></div>`;
}

function libraryView() {
  const albums = ui.status.albums;
  const review = currentReview();
  const submitted = presentedUpload();
  const processing = submitted?.requested.length ?? actionableTitles().length;
  const notIncluded = submitted ? (submitted.reviewSkipped?.length ?? 0)
    : review?.state === "completed" ? selectedTitles().length - processing : 0;
  const job = ui.status.job;
  const showJob = job && (
    (["scan", "prepare"].includes(job.operation) && job.state !== "completed")
    || ["upload", "existing", "recreate"].includes(job.operation)
    || (job.operation === "inspect" && (["running", "stopping"].includes(job.state)
      || (job.id === review?.id && ["failed", "stopped"].includes(job.state))))
  );
  return `${intro(3)}
    ${preparingReview() ? `<section class="panel job" aria-live="polite">
      <h2><span class="spinner"></span> ${tx("preparingReview")}</h2><p class="hint">${tx("checkingNote")}</p></section>`
      : showJob ? jobView(["inspect", "scan", "prepare", "upload", "existing", "recreate"]) : ""}
    ${metrics()}<section class="panel library">
    <div class="section-head review-heading"><div><h2>${tx("localChecks")}</h2>
      <p class="review-summary">${tx(submitted ? "submittedSummary" : "selectedCount", {
        count: submitted ? processing : selectedTitles().length,
      })}${!submitted && review?.state === "completed" && selectedTitles().length
        ? ` · ${tx("actionSummary", { count: processing })}` : ""}</p></div>
      ${reviewPrimaryAction()}</div>
    ${!submitted && review?.state === "completed" && !selectedTitles().length
      ? `<p class="review-notice">${tx("noSelection")}</p>` : ""}
    ${notIncluded ? `<p class="review-notice review-warning" role="status">${tx("selectedNotIncluded", { count: notIncluded })}</p>` : ""}
    <div class="selection-actions review-tools">
      ${button("select-all", "selectAll", "button quiet small", locked() || !selectableNewTitles().length)}
      ${button("clear-selection", "clearSelection", "button quiet small", locked() || !!submitted || review?.state !== "completed" || !ui.selected.size)}
      ${button("scan", "rescan", "button quiet small", locked() || ui.taskStatusUnknown || recoveryNeedsAttention())}
      ${button("recheck-review", "recheck", "button quiet small", locked() || ui.taskStatusUnknown || recoveryNeedsAttention())}</div>
    ${review?.state === "failed" && job?.id !== review.id ? `<div class="task-error" role="alert"><strong>${tx("checkFailed")}</strong><p>${esc(review.error ?? "")}</p></div>` : ""}
    ${!review && !submitted && !active() && !ui.pending ? `<p class="review-notice">${tx("returnToReview")}</p>` : ""}
    ${albums.length ? `<div class="review-list" role="list">${albums.map((album, index) => {
        const result = review?.results?.find(item => item.title === album.title);
        const controlsDisabled = locked() || !!submitted || review?.state !== "completed" || !!album.audioIssue;
        const inBatch = submitted?.requested.includes(album.title);
        const batchKey = submitted?.completed.includes(album.title) ? "batchProcessed"
          : submitted?.skipped.some(item => item.title === album.title) ? "batchSkipped"
          : inBatch ? ["running", "stopping"].includes(submitted.state) ? "batchIncluded" : "batchNotProcessed"
          : submitted?.reviewSkipped?.includes(album.title) ? "batchNotSent" : "notInBatch";
        const remote = submitted ? tx(batchKey)
          : !result ? tx((preparingReview() || (job?.operation === "inspect"
              && ["running", "stopping"].includes(job.state))) ? "waitingForCheck" : "presence_unchecked")
          : result.error != null ? `${tx("detailsUnavailable")}: ${result.error === "PREVIOUS_CARD_RENAMED" ? tx("previousCardRenamed") : esc(result.error)}`
          : result.presence === "duplicate" ? tx("presence_duplicate")
          : result.presence === "missing" ? `<p>${tx(result.pendingOverwrite ? "pendingOverwriteMissing"
              : result.pendingCreate ? "priorCreateNeedsConfirmation"
              : review.state !== "completed" ? "missingOnYoto"
              : ui.selected.has(album.title) ? "newWillCreate" : "newNotSelected")}</p>
             ${countComparison(album, result)}${result.pendingCreate ? `<p class="review-warning">${tx("pendingCreateMissing")}</p>` : ""}`
          : `${countComparison(album, result)}
             <fieldset class="review-choice"><legend>${tx("existingChoice")}</legend>
               <label><input type="radio" name="existing-action-${index}" value="skip" data-title="${esc(album.title)}"${!ui.overwrites.has(album.title) ? " checked" : ""}${disabled(controlsDisabled)}> ${tx("skipExisting")}</label>
               <label><input type="radio" name="existing-action-${index}" value="replace" data-title="${esc(album.title)}"${ui.overwrites.has(album.title) ? " checked" : ""}${disabled(controlsDisabled)}> ${tx(result.pendingCreate ? "resumeCreation" : result.pendingOverwrite ? "resumeOverwrite" : "replaceExisting")}</label>
             </fieldset>${result.pendingOverwrite ? `<p>${tx("pendingOverwrite")}</p>` : ""}`;
        return `<article class="review-row" role="listitem"><div class="review-card"><input type="checkbox" name="selected-card"
        data-title="${esc(album.title)}" aria-label="${tx("selectCard", { album: album.title })}"${(submitted ? inBatch : ui.selected.has(album.title)) ? " checked" : ""}${disabled(controlsDisabled)}>
        <img src="${album.coverStatus === "provided" ? `/api/cover?index=${index}&v=${ui.status.scanRevision ?? 0}` : "/myo-placeholder.svg"}" alt="" loading="lazy">
        <div><strong>${esc(album.title)}</strong><p>${tx("localCounts", { audio: album.tracks, icons: album.icons, covers: album.coverStatus === "provided" ? 1 : 0 })}</p>${stateBadge(album)}</div></div>
        <div class="review-details">${remote}</div></article>`;
      }).join("")}</div>` :
    `<div class="empty">${svg("sound")}<h3>${tx("empty")}</h3><p>${tx("emptyDetail")}</p></div>`}
    </section><p class="hint">${tx("reviewCountsNote")}</p><p class="hint">${tx("uploadNote")}</p>${footer()}`;
}

function authView() {
  const config = ui.draft;
  const callback = `http://127.0.0.1:${config.oauth_port}/callback`;
  const connection = ui.status.connection;
  const migration = ui.status.accountMigration;
  return `${intro(2)}<section class="panel auth-panel">
    <div class="instruction"><span class="instruction-number">1</span><div>
      <h2>${tx("setupA")}</h2><p>${tx("setupAText")}</p>
      <ul class="setup-bullets"><li>${tx("setupBulletClient")}</li><li>${tx("setupBulletCallback")}</li>
        <li>${tx("setupBulletScope")}</li><li>${tx("setupBulletCopy")}</li></ul>
      <label>${tx("setupB")}</label>
      <div class="copy-field"><code id="callback">${esc(callback)}</code>${button("copy", "copy", "button quiet small")}</div>
      <details class="optional-setting" data-detail="portAdvanced"${ui.portAdvanced ? " open" : ""}>
        <summary>${tx("portAdvanced")}</summary><label for="oauth_port">${tx("port")}</label><input id="oauth_port" class="short" type="number" min="1024" max="65535"
        data-field="oauth_port" value="${esc(config.oauth_port)}"${disabled(locked())}><p class="hint">${tx("portHint")}</p></details>
      <div class="actions">${external("https://dashboard.yoto.dev/", "dashboard", "button secondary small")}
        <button type="button" class="button secondary small" data-action="setup-help" aria-expanded="${ui.setupHelp}">${tx("setupQuestion")}</button></div></div></div>
    ${ui.setupHelp ? `<div class="setup-illustration"><p class="hint">${tx("setupHelpNote")}</p>
      ${["create", "callback", "scopes"].map((image, index) => `<section class="setup-example">
        <h3>${tx(`setupStep${index + 1}`)}</h3><p>${tx(`setupStep${index + 1}Text`)}</p>
        <img src="/setup-${image}.png" alt="${tx(`setupStep${index + 1}`)}" loading="lazy"></section>`).join("")}
      <section class="setup-example"><h3>${tx("setupStep4")}</h3><p>${tx("setupStep4Text")}</p>
        <div class="client-id-example" role="img" aria-label="${tx("clientIdExampleAlt")}">
          <p>${tx("exampleOnly")}</p><strong>My Applications</strong><div class="client-id-row">
            <div><strong>${tx("exampleApplication")}</strong><span>Client ID</span><code>example-client-id</code></div>
            <span class="copy-example">${svg("copy")}</span></div><p>${tx("clientIdExampleHint")}</p></div>
      </section></div>` : ""}</section>
    <section class="panel auth-panel">
    <div class="instruction"><span class="instruction-number">2</span><div><h2>${tx("setupD")}</h2>
      <label for="client_id">${tx("clientLabel")}</label><input id="client_id" data-field="client_id" autocomplete="off" spellcheck="false"
        value="${esc(config.client_id)}" placeholder="${tx("clientPlaceholder")}"${disabled(locked())}><p class="hint">${tx("clientHint")}</p>
      <div class="actions">${button("auth", connection.saved ? "reconnect" : "connect", "button primary", locked() || !config.client_id.trim())}
        ${connectionBadge()}</div>
      <p class="hint">${tx("automaticAccounts")}</p>
      ${connection.error ? `<div class="task-error" role="alert"><strong>${tx("savedLoginNeedsAttention")}</strong><p>${esc(connection.error)}</p></div>` : ""}
    </div></div></section>
    ${migration ? "" : (ui.startingOperation === "auth" || ui.awaitingOperation === "auth")
      && !(ui.status.job?.operation === "auth" && ["running", "stopping"].includes(ui.status.job.state))
      ? `<section class="panel job" aria-live="polite"><h2><span class="spinner"></span>${tx("preparingConnection")}</h2></section>`
      : jobView(["auth"])}
    <section class="panel account-panel">
      <details class="optional-setting" data-detail="accountOptions"${ui.accountOptions ? " open" : ""}>
      <summary>${tx("accountOptions")}</summary>
      ${button("switch-account", connection.saved ? "switchAccount" : "differentAccount", "button secondary small", locked() || !config.client_id.trim())}
      <p class="hint">${tx("switchAccountHint")}</p>
      ${connection.expiresAt ? `<p class="hint">${tx("expiry", { time: new Date(connection.expiresAt * 1000).toLocaleString(ui.language === "zh" ? "zh-CN" : "en-US") })}</p>` : ""}
      ${connection.saved ? button("disconnect", "disconnect", "button quiet small", locked()) : ""}</details>
      <details class="optional-setting" data-detail="iconsAdvanced"${ui.iconsAdvanced ? " open" : ""}>
        <summary>${tx("iconsAdvanced")}</summary><label class="checkbox"><input type="checkbox" data-field="icons_scope"${config.icons_scope ? " checked" : ""}${disabled(locked())}>${tx("iconsScope")}</label>
        <p class="hint">${tx("iconsScopeHint")}</p></details>
    </section>${footer()}`;
}

function verifyView() {
  const upload = overviewUpload();
  const inspection = currentInspection();
  const titles = overviewTitles();
  const finished = upload ? new Set([...upload.completed, ...upload.skipped.map(item => item.title)]) : new Set();
  const unfinished = upload ? upload.requested.filter(title => !finished.has(title)).length : 0;
  const canInspect = !locked() && !ui.taskStatusUnknown && hasCurrentConnection() && hasCurrentScan()
    && !recoveryNeedsAttention() && inspectableTitles().length > 0;
  const inProgress = ["running", "stopping"].includes(ui.status.job?.state);
  return `${intro(4)}<section class="panel">
    ${ui.readOnlyReview ? `<h2>${tx("readOnlySummary", { count: titles.length })}</h2>
      ${inspection ? "" : `<div class="task-error" role="alert"><strong>${tx("reviewStale")}</strong><p>${tx("reviewStaleDetail")}</p></div>`}` :
    upload ? `<h2>${tx("batchSummary", { done: upload.completed.length,
      skipped: upload.skipped.length, unfinished, total: upload.requested.length })}</h2>
      ${upload.reviewSkipped?.length ? `<p class="review-notice review-warning" role="status">${tx("selectedNotIncluded", { count: upload.reviewSkipped.length })}</p>` : ""}
      ${["running", "stopping"].includes(upload.state) ? `<p class="hint">${tx("batchRunning")}</p>` : ""}
      ${upload.state === "stopped" ? `<p class="hint">${tx("batchStopped")}</p>` : ""}
      ${upload.state === "failed" && !recoveryNeedsAttention() ? `<div class="task-error" role="alert"><strong>${tx("batchFailed")}</strong><p>${upload.errorCode && messages[ui.language][upload.errorCode] ? tx(upload.errorCode) : esc(upload.error ?? "")}</p></div>` : ""}` :
    `<h2>${tx("noSessionUpload")}</h2>`}
    <p class="hint">${tx("sessionOnly")}</p>
    ${recoveryNeedsAttention() ? `<div class="task-error"><strong>${tx("recoveryTitle")}</strong><p>${tx("resolveRecovery")}</p>
      <button class="button secondary small" data-step="3">${tx("step3")}</button></div>` :
    !hasCurrentConnection() ? `<p class="hint">${tx("connectForCheck")}</p>` : ""}
    ${inspection?.state === "failed" ? `<div class="task-error" role="alert"><strong>${tx("checkFailed")}</strong><p>${esc(inspection.error ?? "")}</p></div>` : ""}
    ${inspection?.state === "stopped" ? `<p class="hint">${tx("checkStopped")}</p>` : ""}
    <div class="actions">${button("inspect", "recheck", "button secondary small", !canInspect)}
      ${external("https://my.yotoplay.com/cards", "openCards", "button quiet small")}
      ${button("scan", "rescan", "button quiet small", locked() || ui.taskStatusUnknown || recoveryNeedsAttention())}</div>
    <p class="hint">${tx("localPreviewNote")}</p>
  </section>
  ${inProgress ? jobView(["upload", "existing", "recreate", "inspect", "scan", "prepare"]) : ""}
  ${assetOverview(titles, upload, inspection)}${footer()}`;
}

function assetOverview(titles, upload, inspection) {
  const coverReasons = { missing: "coverMissing", ambiguous: "coverAmbiguous", selected_missing: "coverSelectedMissing" };
  const iconReasons = { missing: "iconMissing", ambiguous: "iconAmbiguous", selected_missing: "iconSelectedMissing" };
  const audioReasons = { missing: "noAudio", too_many: "tooManyAudio", untagged_in_mixed_folder: "mixedAudio" };
  return `<section class="panel asset-overview"><div class="section-head"><h2>${tx("overviewTitle")}</h2>
    <span>${tx("selectedCount", { count: titles.length })}</span></div>
    ${ui.status.assetStatusError ? `<div class="task-error" role="alert">${esc(ui.status.assetStatusError)}</div>` : ""}
    ${titles.length ? `<div class="asset-list">${titles.map(title => {
      const index = ui.status.albums.findIndex(album => album.title === title);
      const album = ui.status.albums[index];
      const result = inspection?.results?.find(item => item.title === title);
      const skipped = upload?.skipped.find(item => item.title === title);
      const batchKey = ui.readOnlyReview ? "readOnlyNotSent" : upload ? upload.completed.includes(title) ? "batchProcessed"
        : skipped ? "batchSkipped" : upload.reviewSkipped?.includes(title) ? "batchNotSent"
        : ["running", "stopping"].includes(upload.state) ? "batchWaiting" : "batchNotProcessed" : null;
      const batch = batchKey ? tx(batchKey) : "";
      const presence = result ? tx(`presence_${result.presence}`) : tx("presence_unchecked");
      if (!album) return `<article class="asset-item"><div class="asset-content"><h3>${esc(title)}</h3>
        <p class="asset-message">${batch}</p><p class="asset-message">${tx("sourceUnavailable")}</p></div></article>`;
      const missing = album.trackDetails.filter(track => !track.hasIcon).length;
      const albumName = esc(album.title);
      const unmatchedFiles = (album.unmatchedIcons ?? []).map(path => path.split(/[\\/]/).pop()).join(", ");
      const coverPreview = album.coverStatus === "provided" || result?.cover === "placeholder"
        ? `/api/cover?index=${index}&v=${ui.status.scanRevision ?? 0}` : "/myo-placeholder.svg";
      const trackCount = Math.max(album.trackDetails.length, result?.tracks?.length ?? 0);
      return `<article class="asset-item"><div class="cover-preview"><img src="${coverPreview}" alt="" loading="lazy">
        <span>${tx("localPreview")}</span></div><div class="asset-content">
        <div class="asset-heading"><h3>${albumName}</h3><span class="ready ${result?.presence === "present" && !result.error ? "" : "attention"}">${presence}</span></div>
        ${batch ? `<p class="asset-message">${batch}${skipped ? ` · ${tx(skipReason(skipped.reason))}` : ""}</p>` : ""}
        ${result?.error ? `<p class="asset-message error-text">${tx("detailsUnavailable")}: ${esc(result.error)}</p>` : ""}
        <p class="asset-message">${tx("localCounts", { audio: album.tracks, icons: album.icons, covers: album.coverStatus === "provided" ? 1 : 0 })}</p>
        ${result && result.presence === "present" && !result.error ? `<p class="asset-message">${tx(`cover_${result.cover}`)}</p>` : ""}
        ${album.audioIssue ? `<p class="asset-message">${tx(audioReasons[album.audioIssue])}</p>` : ""}
        ${album.coverStatus !== "provided" ? `<p class="asset-message">${tx(coverReasons[album.coverStatus])}</p>` : ""}
        ${album.coverNeedsUpload ? `<p class="asset-message">${tx("coverPending")}</p>` : ""}
        ${album.iconsNeedUpload ? `<p class="asset-message">${tx("iconsPending", { count: album.iconsNeedUpload })}</p>` : ""}
        ${album.pendingCoverUpdate ? `<p class="asset-message">${tx("updateInterrupted")}</p>` : ""}
        ${album.coverStatus !== "provided" && album.coverCandidates?.length ? `<p class="asset-message">${tx("candidates", {
          files: album.coverCandidates.map(path => path.split(/[\\/]/).pop()).join(", "),
        })}</p>` : ""}
        ${unmatchedFiles ? `<p class="asset-message"><strong>${tx("unmatchedIconFiles")}:</strong> ${esc(unmatchedFiles)}</p>` : ""}
        <button type="button" class="button secondary small" data-action="choose-cover" data-album="${albumName}"
          aria-label="${tx("chooseCoverFor", { album: album.title })}"${disabled(locked() || !hasCurrentScan() || recoveryNeedsAttention())}>${tx("chooseCover")}</button>
        ${trackCount ? `<details class="asset-tracks" data-overview="${albumName}"${ui.expanded.has(album.title) ? " open" : ""}>
          <summary>${missing ? tx("missingTrackCount", { count: missing }) : tx("reviewIcons")}</summary>
          <ul>${Array.from({ length: trackCount }, (_, trackIndex) => {
            const track = album.trackDetails[trackIndex];
            const remote = result?.tracks?.[trackIndex];
            const checked = result?.presence === "present" && !result.error && remote;
            const name = track?.name ?? remote?.name ?? "";
            return `<li><div class="track-preview">${track?.hasIcon ? `<img src="/api/icon?index=${index}&track=${trackIndex}&v=${ui.status.scanRevision ?? 0}" alt="" loading="lazy">` :
              `<span class="empty-icon" aria-hidden="true">♫</span>`}
              <div><strong>${esc(name)}</strong>
                ${remote?.remoteTitle && remote.remoteTitle !== name ? `<span>${tx("remoteTrack", { name: remote.remoteTitle })}</span>` : ""}
                ${track ? `<span>${track.hasIcon ? tx("matchedIcon") : tx(iconReasons[track.issue] ?? "iconMissing")}</span>` : ""}
                <span>${tx(checked ? `audio_${remote.audio}` : "audio_unchecked")}</span>
                <span>${tx(checked ? `icon_${remote.icon}` : "icon_unchecked")}</span>
                ${track?.candidates?.length ? `<span>${tx("candidates", { files: track.candidates.map(path => path.split(/[\\/]/).pop()).join(", ") })}</span>` : ""}</div></div>
              ${track ? `<button type="button" class="button quiet small" data-action="choose-icon" data-album="${albumName}" data-track="${trackIndex}"
                aria-label="${tx("chooseIconFor", { track: track.name })}"${disabled(locked() || !hasCurrentScan() || recoveryNeedsAttention())}>${tx("chooseIcon")}</button>` : ""}</li>`;
          }).join("")}</ul></details>` : ""}</div></article>`;
    }).join("")}</div>` : `<p class="hint">${tx("overviewEmpty")}</p>`}
    </section>`;
}

function skipReason(reason) {
  return {
    missing: "noAudio", too_many: "tooManyAudio", untagged_in_mixed_folder: "mixedAudio",
    not_existing: "notOnYoto", already_existing: "alreadyOnYoto",
  }[reason] ?? "blocked";
}

function jobView(operations) {
  const job = ui.status.job;
  if (!job || !operations.includes(job.operation)) return "";
  const isActive = ["running", "stopping"].includes(job.state);
  const scanning = ["scan", "prepare"].includes(job.operation) || job.phase === "account-scan";
  if (scanning && job.state === "completed") return "";
  const key = { running: "working", stopping: "stopping", completed: "completed", stopped: "stopped", failed: "failed" }[job.state];
  const upload = ["upload", "existing", "verify", "inspect", "recreate"].includes(job.operation);
  const inspecting = job.operation === "inspect";
  const login = validatedLoginURL(job.loginUrl);
  const recovery = job.state === "failed" && job.recovery?.type === "missing_prior_card" ? job.recovery : null;
  return `<section class="panel job ${job.state}" aria-live="polite">
    <div class="section-head"><h2>${isActive ? '<span class="spinner"></span>' : svg(job.state === "completed" ? "check" : "book")}${tx(scanning && job.state === "running" ? "scanning" : inspecting && job.state === "running" ? "checking" : key)}</h2>
      ${isActive ? button("stop", scanning ? "cancelScan" : job.operation === "auth" ? "cancel" : "stop",
                          "button secondary small", ui.pending || job.state === "stopping") : ""}</div>
    ${job.currentAlbum ? `<strong class="current-album">${inspecting ? tx("lastChecked", { album: job.currentAlbum }) : esc(job.currentAlbum)}</strong>` : ""}
    ${scanning && isActive ? `<progress class="indeterminate" aria-label="${tx("scanning")}"></progress><p class="hint">${tx("scanProgress")}</p>` : ""}
    ${upload ? `<div class="progress-label"><span>${tx(inspecting ? "checkedCount" : "completedCount", { done: job.completed.length, total: job.total })}</span>${inspecting ? "" : `<span>${progress(job)}%</span>`}</div>
      ${inspecting && job.total > 0 ? `<progress max="${job.total}" value="${job.completed.length}"></progress>`
        : inspecting ? `<progress class="indeterminate" aria-label="${tx("checking")}"></progress>`
        : `<progress max="100" value="${progress(job)}"></progress>`}` : ""}
    ${inspecting && isActive ? `<p class="hint">${tx("checkingNote")}</p>` : ""}
    ${job.skipped?.length ? `<div class="hint">${tx("skippedCount", { count: job.skipped.length })}
      <ul>${job.skipped.map(item => `<li>${esc(item.title)}: ${tx(skipReason(item.reason))}</li>`).join("")}</ul></div>` : ""}
    ${(job.operation !== "auth" || isActive || job.state === "completed") && (!inspecting || isActive)
      ? `<p class="hint">${tx(`phase_${job.phase}`)}</p>` : ""}
    ${job.audioTotal && isActive ? `<p class="hint">${tx("audioCount", { done: job.audioDone, total: job.audioTotal })}</p>` : ""}
    ${job.operation === "auth" && job.phase === "auth" && login && job.state === "running" ?
      `<a class="button primary" href="${esc(login)}" target="_blank" rel="noopener noreferrer">${tx("login")} ↗</a><p>${tx("loginHint")}</p>` : ""}
    ${recovery ? `<div class="task-error"><strong>${tx("recoveryTitle")}</strong>
      <p>${tx("recoveryDetail", { album: recovery.title })}</p>
      ${recovery.priorStatus === "pending_create" ? `<p>${tx("recoveryPending")}</p>` : ""}
      <p>${tx("recoveryInstructions")}</p>
      <div class="actions">${external("https://my.yotoplay.com/cards", "openCards", "button secondary small")}
      <button type="button" class="button primary small" data-action="recreate"${disabled(!canResolveRecovery() || !hasRecoverableCard(recovery.title))}>${tx("recreate")}</button></div></div>`
      : job.error ? `<div class="task-error"><strong>${tx("errorTitle")}</strong><p>${job.errorCode && messages[ui.language][job.errorCode] ? tx(job.errorCode) : esc(job.error)}</p><button class="text-link" data-action="guide">${tx("troubleshooting")}</button></div>` : ""}
    ${job.operation === "recreate" && job.state === "completed" ? `<p class="hint">${tx("recreateContinue")}</p>` : ""}
    ${ui.step === 3 && ["upload", "existing"].includes(job.operation)
      && ["stopped", "failed"].includes(job.state) && !recovery ? `<p class="hint">${tx("resumeAfterPause")}</p>` : ""}
    ${isActive && upload && job.operation !== "inspect" ? `<p class="hint">${tx("stopNote")}</p>` : ""}
  </section>`;
}

function guideView() {
  return `<div class="eyebrow">${tx("brand")} · ${tx("guide")}</div><h1>${tx("guideTitle")}</h1><p class="lead">${tx("guideDesc")}</p>
    <section class="panel guide-start"><h2>${tx("guideStart")}</h2><p>${tx("guideStartText")}</p>${external("https://www.python.org/downloads/", "python")}</section>
    <div class="guide-steps">${["Folder", "Account", "Review", "Verify"].map((key, index) =>
      `<section class="panel"><span class="guide-index">0${index + 1}</span><div><h2>${tx(`guide${key}`)}</h2><p>${tx(`guide${key}Text`)}</p>
        <button class="text-link" data-step="${index + 1}">${tx(`step${index + 1}`)} ${svg("arrow")}</button></div></section>`).join("")}</div>
    <section class="panel"><h2>${tx("troubleshooting")}</h2>${["Callback", "Scopes", "Source", "Resume"].map(key =>
      `<details class="faq"><summary>${tx(`issue${key}`)}</summary><p>${tx(`fix${key}`)}</p></details>`).join("")}</section>
    <div class="guide-bottom"><section class="callout">${svg("shield")}<div><h2>${tx("guidePrivacy")}</h2><p>${tx("guidePrivacyText")}</p></div></section>
      <section class="callout"><div><h2>${tx("guideLimits")}</h2><p>${tx("guideLimitsText")}</p></div></section></div>`;
}

function render() {
  if (!ui.status) return;
  document.documentElement.lang = ui.language === "zh" ? "zh-CN" : "en";
  document.title = `${t("brand")} · ${t(ui.view === "guide" ? "guide" : `step${ui.step}`)}`;
  const focus = document.activeElement?.id;
  const selection = document.activeElement?.selectionStart;
  const views = [sourceView, authView, libraryView, verifyView];
  app.innerHTML = `<header class="topbar"><a class="brand" href="#" data-action="home"><img src="/icon.svg" alt=""><span>${tx("brand")}<small>${tx("tagline")}</small></span></a>
    <nav aria-label="${tx("wizard")}"><button data-action="home" class="${ui.view === "wizard" ? "active" : ""}">${tx("wizard")}</button>
      <button data-action="guide" class="${ui.view === "guide" ? "active" : ""}">${svg("book")}${tx("guide")}</button></nav>
    <div class="top-actions"><select id="language" aria-label="${tx("language")}"><option value="en"${ui.language === "en" ? " selected" : ""}>English</option>
      <option value="zh"${ui.language === "zh" ? " selected" : ""}>简体中文</option></select>
      <button class="quit" data-action="quit"${disabled(active())}>${tx("quit")}</button></div></header>
    <div class="layout"><aside class="sidebar"><div class="local-badge"><span class="dot"></span>${tx("local")}</div>
      <h2>${tx("sidebarTitle")}</h2><p>${tx("sidebarText")}</p><ol class="stepper">${[1, 2, 3, 4].map(n =>
        `<li class="${ui.step === n && ui.view === "wizard" ? "current" : ""}"><button data-step="${n}"${ui.step === n ? ' aria-current="step"' : ""}${disabled(!canNavigate(n))}>
          <span>${n}</span><strong>${tx(`step${n}`)}</strong></button></li>`).join("")}</ol>
      <div class="sidebar-help"><strong>${tx("help")}</strong><button class="text-link" data-action="guide">${tx("helpLink")} ↗</button></div>
      <p class="privacy">${svg("shield")}${tx("privacy")}</p></aside>
    <main id="main"><div class="persistent-alerts">
      ${ui.taskStatusUnknown ? `<section class="task-status-warning" role="alert"><strong>${tx("taskStatusTitle")}</strong>
        <p>${tx("taskStatusUnknown")}</p>${button("dismiss-task-warning", "dismiss", "button quiet small")}</section>` : ""}
      ${accountConfirmation()}</div>
      ${(ui.error || ui.offline) && !accountPending() ? `<div class="error-banner" role="alert"><strong>${tx("errorTitle")}</strong><p>${ui.offline ? tx("serverOffline") : esc(ui.error)}</p>${button("refresh", "retry", "button quiet small")}</div>` : ""}
      ${ui.notice ? `<div class="notice" role="status">${esc(ui.notice)}</div>` : ""}
      ${ui.view === "guide" ? guideView() : views[ui.step - 1]()}</main></div>`;
  const input = focus && document.getElementById(focus);
  if (input) {
    input.focus({ preventScroll: true });
    if (typeof selection === "number" && ["text", ""].includes(input.type)) input.setSelectionRange(selection, selection);
  }
}

let refreshRequested = 0;
let refreshApplied = 0;

function trackStartedJob(job) {
  ui.awaitingJobID = job.id;
  ui.awaitingOperation = job.operation;
  ui.awaitingAfterStatusSequence = refreshRequested;
}

async function refresh(force = false) {
  const sequence = ++refreshRequested;
  const status = await api("/api/status");
  if (sequence < refreshApplied) return;
  refreshApplied = sequence;
  const changed = JSON.stringify(status) !== JSON.stringify(ui.status);
  const recovered = ui.offline;
  ui.offline = false;
  const previous = ui.status;
  ui.status = status;
  if (previous?.reviewInspection?.id !== status.reviewInspection?.id) {
    clearExistingChoices();
    if (!status.reviewInspection) ui.automaticReviewKey = null;
  }
  if (!ui.draft) ui.draft = { ...status.config };
  else if (previous?.config.workspace !== status.config.workspace) ui.draft.workspace = status.config.workspace;
  if (status.accountMigration && !previous?.accountMigration) { ui.step = 2; ui.view = "wizard"; }
  const collection = `${status.config.workspace}\n${status.config.root}`;
  if (collection !== ui.selectionCollection) {
    if (previous?.config.root !== status.config.root) { ui.selected.clear(); ui.seen.clear(); }
    ui.selectionCollection = collection;
  }
  const unfinishedScan = (ui.awaitingJobID && status.job?.id !== ui.awaitingJobID
      && ["scan", "prepare"].includes(ui.awaitingOperation))
    || (["scan", "prepare"].includes(status.job?.operation) && status.job?.state !== "completed")
    || (status.job?.operation === "auth" && status.job?.phase === "account-scan" && status.job?.state !== "completed");
  if (!unfinishedScan) {
    const available = new Set(status.albums.filter(album => !album.audioIssue).map(album => album.title));
    for (const title of available) if (!ui.seen.has(title)) ui.selected.add(title);
    for (const title of ui.selected) if (!available.has(title)) ui.selected.delete(title);
    ui.seen = available;
    const review = currentReview();
    if (review?.state === "completed") {
      for (const result of review.results ?? []) {
        if (result.presence === "present" && result.error == null && !ui.overwrites.has(result.title)) {
          ui.selected.delete(result.title);
        }
      }
    }
  }
  const job = status.job;
  let lostTask = false;
  const expectedID = ui.awaitingJobID;
  if (expectedID && (job?.id === expectedID
      || (ui.awaitingAfterStatusSequence !== null && sequence > ui.awaitingAfterStatusSequence))) {
    lostTask = job?.id !== expectedID;
    ui.awaitingJobID = null;
    ui.awaitingOperation = null;
    ui.awaitingAfterStatusSequence = null;
    if (lostTask) {
      if (ui.reviewAfterScanID === expectedID) ui.reviewAfterScanID = null;
      if (ui.openLoginAfterStartID === expectedID) ui.openLoginAfterStartID = null;
      if (ui.reviewAfterLoginID === expectedID) ui.reviewAfterLoginID = null;
      if (ui.resultsAfterUploadID === expectedID) ui.resultsAfterUploadID = null;
      ui.taskStatusUnknown = true;
    }
  }
  if (job?.id && job.id === ui.reviewAfterScanID && !["running", "stopping"].includes(job.state)) {
    ui.reviewAfterScanID = null;
    if (job.state === "completed") {
      if (status.albums.length) {
        ui.step = hasCurrentConnection() ? 3 : 2;
        ui.view = "wizard";
      } else {
        ui.error = t("scanEmpty");
      }
    }
  }
  if (job?.id && job.id === ui.openLoginAfterStartID) {
    if (job.state === "running" && job.loginUrl) {
      ui.openLoginAfterStartID = null;
      const login = validatedLoginURL(job.loginUrl);
      if (login) window.open(login, "_blank", "noopener,noreferrer");
      else ui.error = t("invalidLoginLink");
    } else if (job.state !== "running") {
      ui.openLoginAfterStartID = null;
    }
  }
  if (job?.id && job.id === ui.reviewAfterLoginID && !["running", "stopping"].includes(job.state)
      && !status.accountMigration) {
    ui.reviewAfterLoginID = null;
    if (job.state === "completed" && hasCurrentConnection() && hasCurrentScan()) ui.step = 3;
  }
  if (job?.id && job.id === ui.resultsAfterUploadID && !["running", "stopping"].includes(job.state)) {
    ui.resultsAfterUploadID = null;
    if (job.state === "completed") ui.step = 4;
  }
  if (changed || force || recovered || lostTask) render();
  await inspectReviewIfNeeded();
  await inspectOverviewIfNeeded();
}

async function save() {
  const submitted = { ...ui.draft, oauth_port: Number(ui.draft.oauth_port) };
  await api("/api/config", submitted);
  await refresh();
  for (const field of ["root", "workspace"]) {
    if (ui.draft[field] === submitted[field]) ui.draft[field] = ui.status.config[field];
  }
}

async function startOperation(action, reviewWhenComplete = false, inspectionTitles = null, purpose = null) {
  let operation = action === "switch-account" ? "auth" : action;
  const titles = action === "upload" ? actionableTitles() : action === "inspect" ? inspectionTitles : null;
  const review = action === "upload" ? currentReview() : null;
  const overwrites = action === "upload" ? titles.filter(title => ui.overwrites.has(title)) : [];
  const previous = action === "recreate" ? ui.status.job : null;
  if (action === "upload") {
    if (review?.state !== "completed" || !titles.length || !hasCurrentScan() || !hasCurrentConnection()
        || active() || ui.offline || ui.taskStatusUnknown) {
      throw new Error(t("uploadNeed"));
    }
    const resumes = overwrites.filter(title => review.results?.find(item => item.title === title)?.pendingCreate);
    const overwriteChecks = overwrites.filter(title => review.results?.find(item => item.title === title)?.pendingOverwrite);
    const replacements = overwrites.filter(title => !resumes.includes(title) && !overwriteChecks.includes(title));
    const creates = titles.filter(title => !overwrites.includes(title));
    const confirmation = [
      creates.length ? t("uploadConfirmCreate", { cards: creates.join("\n") }) : "",
      replacements.length ? t("uploadConfirmReplace", { cards: replacements.join("\n") }) : "",
      resumes.length ? t("uploadConfirmResume", { cards: resumes.join("\n") }) : "",
      overwriteChecks.length ? t("uploadConfirmOverwriteCheck", { cards: overwriteChecks.join("\n") }) : "",
    ].filter(Boolean).join("\n\n");
    if (!window.confirm(confirmation)) return;
  }
  if (action === "inspect" && (!titles?.length || !hasCurrentScan() || !hasCurrentConnection()
      || active() || ui.offline || ui.taskStatusUnknown || recoveryNeedsAttention()
      || titles.some(title => !ui.status.albums.some(album => album.title === title)))) {
    throw new Error(t("checkUnavailable"));
  }
  if (action === "recreate") {
    const recovery = previous?.recovery;
    if (previous?.state !== "failed" || previous.operation !== "upload"
        || recovery?.type !== "missing_prior_card" || !hasRecoverableCard(recovery.title)
        || !hasCurrentScan() || !hasCurrentConnection() || active() || ui.offline || ui.taskStatusUnknown) {
      throw new Error(t("RECREATE_CONFIRMATION_REQUIRED"));
    }
    if (!window.confirm(t("recreateConfirm", { album: recovery.title }))) return;
  }
  await save();
  const body = action === "recreate"
    ? { operation, album: previous.recovery.title, confirmed: true, recoveryJobId: previous.id }
    : { operation, album: null };
  if (action === "switch-account") body.switchAccount = true;
  if (titles) body.albums = titles;
  if (action === "inspect" && purpose === "review") body.purpose = "review";
  if (action === "upload") {
    if (ui.taskStatusUnknown || currentReview()?.id !== review.id || titles.length !== actionableTitles().length
        || titles.some(title => !actionableTitles().includes(title))) throw new Error(t("INVALID_REVIEW"));
    body.reviewId = review.id;
    body.overwriteTitles = overwrites;
    body.skippedTitles = selectedTitles().filter(title => !titles.includes(title));
    body.confirmed = true;
  }
  const { job } = await api("/api/jobs", body);
  if (!job?.id || job.operation !== operation) throw new Error("Invalid task response from the local service.");
  trackStartedJob(job);
  if (reviewWhenComplete) ui.reviewAfterScanID = job.id;
  if (operation === "auth") { ui.openLoginAfterStartID = job.id; ui.reviewAfterLoginID = job.id; }
  if (operation === "upload") ui.resultsAfterUploadID = job.id;
  await refresh(true);
}

async function inspectReviewIfNeeded(force = false) {
  if (ui.view !== "wizard" || ui.step !== 3 || locked() || ui.taskStatusUnknown || !hasCurrentScan()
      || !hasCurrentConnection() || recoveryNeedsAttention()) return;
  const titles = ui.status.albums.map(album => album.title);
  if (!titles.length) return;
  const key = JSON.stringify([ui.status.scanRevision ?? 0, ui.status.config.workspace, [...titles].sort()]);
  if (!force && waitingAfterUpload()) return;
  if (!force && (currentReview() || ui.automaticReviewKey === key)) return;
  if (force) clearExistingChoices();
  ui.automaticReviewKey = key;
  ui.startingOperation = "inspect";
  ui.pending = true;
  render();
  try { await startOperation("inspect", false, titles, "review"); }
  catch (error) { ui.error = error.message; }
  finally { ui.startingOperation = null; ui.pending = false; if (!ui.closed) render(); }
}

async function inspectOverviewIfNeeded(force = false) {
  if (ui.view !== "wizard" || ui.step !== 4 || ui.readOnlyReview || locked() || ui.taskStatusUnknown || !hasCurrentScan()
      || !hasCurrentConnection() || recoveryNeedsAttention()) return;
  const titles = inspectableTitles();
  if (!titles.length) return;
  const key = JSON.stringify([ui.status.scanRevision ?? 0, ui.status.lastUpload?.id ?? null, [...titles].sort()]);
  if (!force && waitingAfterUpload()) return;
  if (!force && (currentInspection() || ui.automaticInspectionKey === key)) return;
  ui.automaticInspectionKey = key;
  ui.startingOperation = "inspect";
  ui.pending = true;
  render();
  try { await startOperation("inspect", false, titles); }
  catch (error) { ui.error = error.message; }
  finally { ui.startingOperation = null; ui.pending = false; if (!ui.closed) render(); }
}

async function perform(action, data = {}) {
  ui.error = ""; ui.notice = "";
  if (action === "home") { ui.view = "wizard"; render(); return; }
  if (action === "guide") { ui.view = "guide"; render(); window.scrollTo(0, 0); return; }
  if (action === "setup-help") { ui.setupHelp = !ui.setupHelp; render(); return; }
  if (action === "select-all") {
    if (!active() && !ui.offline && !accountPending() && !presentedUpload()) {
      for (const title of selectableNewTitles()) ui.selected.add(title);
    }
    render(); return;
  }
  if (action === "clear-selection") {
    if (!active() && !ui.offline && !accountPending() && !presentedUpload()
        && currentReview()?.state === "completed") {
      ui.selected.clear(); ui.overwrites.clear();
    }
    render(); return;
  }
  if (action === "next") {
    if (ui.step === 1) { await startOperation("scan", true); return; }
    if (ui.step === 3 && !actionableTitles().length) {
      ui.readOnlyReview = { id: currentReview().id, titles: selectedTitles() };
      ui.step = 4; render(); window.scrollTo(0, 0); return;
    }
    ui.step += 1; render(); window.scrollTo(0, 0); return;
  }
  if (action === "back") {
    if (ui.step === 4 && ui.readOnlyReview && canNavigate(3)) {
      ui.readOnlyReview = null; ui.step = 3; render(); window.scrollTo(0, 0); return;
    }
    for (let target = ui.step - 1; target >= 1; target--) {
      if (canNavigate(target)) { ui.readOnlyReview = null; ui.step = target; break; }
    }
    render(); window.scrollTo(0, 0); return;
  }
  if (action === "copy") {
    const text = `http://127.0.0.1:${ui.draft.oauth_port}/callback`;
    const native = window.webkit?.messageHandlers?.yotolio;
    if (native) {
      if (await native.postMessage({ type: "copy", value: text }) !== true) throw new Error(t("copyUnavailable"));
    } else if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      throw new Error(t("copyUnavailable"));
    }
    ui.notice = t("copied"); render(); return;
  }
  if (action === "browse") {
    ui.picker = true; render();
    try { const result = await api("/api/folder", {}); if (result.path) ui.draft.root = result.path; }
    finally { ui.picker = false; render(); }
    return;
  }
  if (action === "choose-cover" || action === "choose-icon") {
    if (recoveryNeedsAttention()) throw new Error(t("RECOVERY_REQUIRED"));
    ui.picker = true; render();
    try {
      const result = await api("/api/asset", {
        album: data.album, kind: action === "choose-cover" ? "cover" : "icon",
        track: action === "choose-icon" ? Number(data.track) : null,
      });
      if (result.chosen) {
        const { job } = await api("/api/jobs", { operation: "scan", album: null });
        if (!job?.id || job.operation !== "scan") throw new Error("Invalid task response from the local service.");
        trackStartedJob(job);
        await refresh(true);
      }
    } finally { ui.picker = false; render(); }
    return;
  }
  if (action === "quit") {
    if (!window.confirm(t("quitConfirm"))) return;
    const native = window.webkit?.messageHandlers?.yotolio;
    if (native) {
      if (await native.postMessage({ type: "quit" }) !== true) throw new Error(t("quitUnavailable"));
      return;
    }
    await api("/api/quit", {}); ui.closed = true;
    app.innerHTML = `<div class="goodbye">${svg("check")}<h1>${tx("brand")}</h1><p>${tx("goodbye")}</p></div>`; return;
  }
  if (action === "disconnect") {
    if (!window.confirm(t("disconnectConfirm"))) return;
    await api("/api/disconnect", {}); await refresh(true); return;
  }
  if (["account-same", "account-different", "account-cancel"].includes(action)) {
    const migration = ui.status.accountMigration;
    if (!migration) throw new Error(t("ACCOUNT_CONFIRMATION_EXPIRED"));
    await api("/api/account-migration", { id: migration.id, choice: action.slice(8) });
    await refresh(true); return;
  }
  if (action === "stop") { await api("/api/stop", {}); await refresh(true); return; }
  if (action === "refresh") { await refresh(true); return; }
  if (action === "save") { await save(); ui.notice = t("saved"); render(); return; }
  if (["prepare", "scan", "auth", "switch-account", "upload", "verify", "recreate"].includes(action)) {
    await startOperation(action);
  }
}

app.addEventListener("input", event => {
  const field = event.target.dataset.field;
  if (field) {
    ui.draft[field] = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    if (field === "client_id" || field === "icons_scope") { render(); return; }
    if (field === "oauth_port") document.querySelector("#callback").textContent = `http://127.0.0.1:${event.target.value}/callback`;
    const next = app.querySelector('[data-action="next"]');
    if (next) next.disabled = !canContinue();
    for (const element of app.querySelectorAll("[data-step]")) {
      element.disabled = !canNavigate(Number(element.dataset.step));
    }
  }
});
app.addEventListener("change", event => {
  if (event.target.id === "language") {
    ui.language = event.target.value;
    try { localStorage.setItem("yoto-language", ui.language); }
    catch (error) { console.warn("Language preference cannot be saved.", error); }
    render();
  }
  if (event.target.name === "selected-card") {
    setSelected(event.target.dataset.title, event.target.checked);
    render();
  }
  if (event.target.name?.startsWith("existing-action-") && !locked()
      && currentReview()?.results?.some(result => result.title === event.target.dataset.title
        && result.presence === "present" && result.error == null)) {
    setSelected(event.target.dataset.title, event.target.value === "replace");
    render();
  }
});
app.addEventListener("toggle", event => {
  if (event.target.dataset.detail) ui[event.target.dataset.detail] = event.target.open;
  if (event.target.dataset.overview !== undefined) {
    if (event.target.open) ui.expanded.add(event.target.dataset.overview);
    else ui.expanded.delete(event.target.dataset.overview);
  }
}, true);
app.addEventListener("click", async event => {
  const target = event.target.closest("[data-action], [data-step]");
  if (!target || target.disabled) return;
  event.preventDefault();
  if (target.dataset.step) {
    const step = Number(target.dataset.step);
    if (canNavigate(step)) {
      if (step !== 4) ui.readOnlyReview = null;
      ui.step = step; ui.view = "wizard"; render(); window.scrollTo(0, 0);
    }
    await inspectReviewIfNeeded();
    await inspectOverviewIfNeeded();
    return;
  }
  if (target.dataset.action === "dismiss-task-warning") {
    ui.taskStatusUnknown = false; render(); return;
  }
  if (ui.pending) return;
  if (target.dataset.action === "inspect") {
    if (ui.readOnlyReview && canNavigate(3)) {
      ui.readOnlyReview = null; ui.step = 3; render();
      await inspectReviewIfNeeded(true);
    } else {
      await inspectOverviewIfNeeded(true);
    }
    return;
  }
  if (target.dataset.action === "recheck-review") { await inspectReviewIfNeeded(true); return; }
  if (target.dataset.action === "next" && !canContinue()) return;
  if (target.dataset.action === "upload" && !canUpload()) return;
  if (["auth", "switch-account"].includes(target.dataset.action)) ui.startingOperation = "auth";
  ui.pending = true;
  render();
  try { await perform(target.dataset.action, target.dataset); }
  catch (error) { ui.error = error.message; render(); }
  finally {
    ui.startingOperation = null;
    ui.pending = false;
    if (!ui.closed) { render(); await inspectReviewIfNeeded(); await inspectOverviewIfNeeded(); }
  }
});

async function initialize() {
  try {
    const bootstrap = await api("/api/bootstrap"); ui.csrf = bootstrap.csrf;
    await refresh();
    if (hasCurrentScan()) { ui.step = hasCurrentConnection() ? 3 : 2; render(); await inspectReviewIfNeeded(); }
  } catch (error) {
    app.innerHTML = `<div class="goodbye"><h1>${tx("errorTitle")}</h1><p>${esc(error.message)}</p><p>${tx("serverOffline")}</p></div>`;
    return;
  }
  let polling = false;
  setInterval(async () => {
    if (polling || ui.closed || ui.pending) return;
    polling = true;
    try { await refresh(); }
    catch (error) { ui.offline = true; console.warn("Local UI connection failed.", error); render(); }
    finally { polling = false; }
  }, 1000);
}
initialize();
