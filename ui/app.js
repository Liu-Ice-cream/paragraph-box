"use strict";

const $ = selector => document.querySelector(selector);
const requestedView = new URLSearchParams(location.search).get("view");
const hashToken = location.hash.slice(1);
const hasSessionToken = /^[A-Za-z0-9_-]{43}$/.test(hashToken);
const token = hasSessionToken ? hashToken : sessionStorage.getItem("paragraphbox-token") || "";
if (hasSessionToken) { sessionStorage.setItem("paragraphbox-token", token); history.replaceState(null, "", location.pathname); }
let saved = null;
let serverData = null;
let busy = false;
let toastTimer;
let rowNumber = 0;
let searchNumber = 0;
let searchController = null;
let indexBusy = false;
let stopSearchProgress = null;
let readerSession = 0;
let readerController = null;
let readerOriginal = null;
let readerContext = null;
let readerOpener = null;
let previewHits = [];
let previewQuery = "";
let readerResults = [];
let readerPosition = -1;
let readerQuery = "";
let readerActionBusy = false;

function queryParts(value) {
  const query = value.trim().replace(/^sn(?:\s+|$)/, "").normalize("NFKC");
  const match = /^@([^\s]*)(?:\s+([\s\S]*))?$/.exec(query);
  return match ? {alias: match[1], keywords: (match[2] || "").trim()} : {alias: null, keywords: query};
}
function scopeQuery(alias, keywords) { return alias === null ? keywords : `@${alias}${keywords ? ` ${keywords}` : ""}`; }
function syncScopes(canonical) {
  const parts = queryParts($("#preview-query").value);
  const selected = canonical === undefined ? parts.alias : canonical;
  for (const button of $("#search-scopes").children) {
    const alias = button.dataset.alias || null;
    const pressed = alias === null ? selected === null : selected !== null && alias.normalize("NFKC").toLowerCase() === selected.normalize("NFKC").toLowerCase();
    button.setAttribute("aria-pressed", String(pressed));
    button.classList.toggle("active", pressed); button.disabled = busy || indexBusy || !serverData;
  }
}
function samePreviewQuery(query) {
  const current = queryParts($("#preview-query").value), requested = queryParts(query);
  return current.alias === requested.alias && current.keywords === requested.keywords;
}
function renderScopes(data, canonical) {
  const make = alias => {
    const button = document.createElement("button"); button.type = "button"; button.className = "scope-chip";
    button.dataset.alias = alias || ""; button.textContent = alias === null ? "全部目录" : alias;
    button.setAttribute("aria-label", alias === null ? "搜索全部目录" : `检索目录：${alias}`);
    if (alias !== null && !data.root_states.find(state => state.alias === alias)?.exists) {
      button.classList.add("unavailable"); button.title = "目录当前不可用，检索后可查看修复建议。";
    }
    button.addEventListener("click", () => chooseScope(alias)); return button;
  };
  $("#search-scopes").replaceChildren(make(null), ...data.config.roots.map(root => make(root.alias)));
  syncScopes(canonical);
  $("#search-scope-help").textContent = data.index_status?.report ? "快捷筛选使用已保存目录；多个关键词需全部命中同一段落或章节。" : "首次搜索会自动建立索引。快捷筛选使用已保存目录。";
}
function chooseScope(alias) {
  if (busy || indexBusy || !serverData) return;
  const {keywords} = queryParts($("#preview-query").value);
  $("#preview-query").value = scopeQuery(alias, keywords); syncScopes();
  if (keywords) search($("#preview-query").value);
  else resetPreview("已选择搜索范围，输入关键词即可检索。");
  $("#preview-query").focus();
}
$("#preview-query").addEventListener("input", () => syncScopes());

function exclusions(row) { return row.querySelector(".exclude-input").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean); }

async function api(path, data, signal, operation) {
  const headers = {Authorization: `Bearer ${token}`};
  if (operation) headers["X-ParagraphBox-Operation"] = operation;
  const options = {headers, signal};
  if (data !== undefined) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) { const error = new Error(body.error || "暂时无法完成操作，请重试。"); error.issues = body.issues || []; throw error; }
  return body;
}
function watchIndex(operation, current, preview = false) {
  const controller = new AbortController();
  let stopped = false, timer;
  const poll = async () => {
    try {
      const data = await api(`/api/index-progress?operation=${encodeURIComponent(operation)}`, undefined, controller.signal);
      const progress = data.progress;
      if (!stopped && current() && progress?.status === "running") {
        const stage = {preparing: "准备索引", waiting: "等待索引更新", scan: "检查资料目录", read: "读取文件", parse: "整理段落", database: "写入索引", commit: "保存索引", cached: "使用最近索引", done: "索引完成"}[progress.stage] || "更新索引";
        const message = `${stage} · 已检查 ${progress.processed} 个文件 · 已用 ${(progress.elapsed_ms / 1000).toFixed(1)} 秒`;
        $("#index-state").textContent = indexBusy ? "正在重建" : "正在检查";
        $("#index-note").textContent = message;
        if (preview) $("#preview-count").textContent = message;
      }
    } catch (error) { if (error.name === "AbortError") return; }
    if (!stopped && current()) timer = setTimeout(poll, 250);
  };
  timer = setTimeout(poll, 150);
  return () => { stopped = true; clearTimeout(timer); controller.abort(); };
}
function notice(message = "") { $("#notice").textContent = message; $("#notice").hidden = !message; }
function toast(message) { clearTimeout(toastTimer); $("#toast span").textContent = message; $("#toast").hidden = false; toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4200); }
function formDocument() {
  return {roots: [...document.querySelectorAll(".root-row")].map(row => ({alias: row.querySelector(".alias-input").value.trim(), path: row.querySelector(".path-input").value.trim(), exclude_dirs: exclusions(row)})), max_results: Number($("#max-results").value), max_file_bytes: Math.round(Number($("#max-size").value) * 1000000), editor: $("#editor").value};
}
function isDirty() { return saved && JSON.stringify(formDocument()) !== JSON.stringify(saved); }
function updateDirty() {
  const dirty = Boolean(isDirty());
  $("#save").disabled = busy || !saved || !dirty;
  $("#revert").disabled = busy || !dirty;
  $("#save-status").lastChild.textContent = !saved ? "配置尚未加载" : dirty ? "有更改尚未保存" : "已与当前配置同步";
  const count = document.querySelectorAll(".root-row").length;
  $("#root-count").textContent = String(count); $("#empty-roots").hidden = count > 0;
  $("#rebuild-index").disabled = busy || !saved || dirty;
}
function rowState(row, state) {
  const badge = row.querySelector(".root-state");
  badge.className = `root-state ${state === "available" ? "" : state}`;
  badge.querySelector("span").textContent = {available: "目录可用", missing: "目录不可用", pending: "尚未保存", invalid: "需要修正"}[state];
}
function showIssues(issues = []) {
  document.querySelectorAll(".field-error").forEach(label => { label.hidden = true; label.textContent = ""; });
  document.querySelectorAll("[aria-invalid]").forEach(field => field.removeAttribute("aria-invalid"));
  const rows = [...document.querySelectorAll(".root-row")];
  rows.forEach(row => rowState(row, row.dataset.state));
  let first = null;
  for (const issue of issues) {
    let field, label;
    if (Number.isInteger(issue.index) && rows[issue.index]) {
      const row = rows[issue.index];
      const name = ["alias", "exclude_dirs"].includes(issue.field) ? issue.field : "path";
      field = row.querySelector({alias: ".alias-input", path: ".path-input", exclude_dirs: ".exclude-input"}[name]);
      label = row.querySelector(`[data-field="${name}"]`);
      if (name === "exclude_dirs") row.querySelector(".exclude-details").open = true;
      rowState(row, issue.field === "path" ? "missing" : "invalid");
    } else if (["max_results", "max_file_bytes", "editor"].includes(issue.field)) {
      const id = {max_results: "max-results", max_file_bytes: "max-size", editor: "editor"}[issue.field];
      field = $(`#${id}`); label = $(`#${id}-error`);
    }
    if (field && label) { field.setAttribute("aria-invalid", "true"); label.textContent = issue.message; label.hidden = false; first ??= field; }
  }
  return first;
}
function setSaving(value) {
  busy = value;
  document.querySelectorAll("#roots input, #roots textarea, #roots button, #max-results, #max-size, #editor, #add-root").forEach(field => { field.disabled = value; });
  updateDirty();
  syncScopes();
}
function rootRow(root, exists = true) {
  const row = $("#root-template").content.firstElementChild.cloneNode(true);
  const alias = row.querySelector(".alias-input"), path = row.querySelector(".path-input");
  alias.value = root.alias; path.value = root.path;
  const exclude = row.querySelector(".exclude-input"); exclude.value = (root.exclude_dirs || []).join("\n");
  const updateExcludes = () => { row.querySelector(".exclude-count").textContent = exclusions(row).length ? `${exclusions(row).length} 项` : "未设置"; };
  updateExcludes();
  row.dataset.state = exists ? "available" : "missing"; rowState(row, row.dataset.state);
  const id = ++rowNumber;
  for (const [name, field] of [["alias", alias], ["path", path], ["exclude_dirs", exclude]]) {
    const label = row.querySelector(`[data-field="${name}"]`);
    label.id = `root-${id}-${name}-error`; field.setAttribute("aria-describedby", label.id);
  }
  const update = () => {
    row.dataset.state = "pending"; showIssues(); notice(); updateExcludes();
    row.querySelector(".alias-example").textContent = `sn @${alias.value.trim() || "别名"} 关键词`;
    row.querySelector(".remove-root").setAttribute("aria-label", `移除${alias.value || "新"}目录`); updateDirty();
  };
  alias.addEventListener("input", update); path.addEventListener("input", update);
  exclude.addEventListener("input", update);
  row.querySelector(".remove-root").addEventListener("click", () => { row.remove(); showIssues(); notice(); updateDirty(); });
  row.querySelector(".browse-button").addEventListener("click", async event => {
    const button = event.currentTarget; button.disabled = true;
    try { const value = await api("/api/folder", {}); if (value.path && row.isConnected && !busy) { path.value = value.path; update(); } }
    catch (error) { notice(error.message); }
    finally { button.disabled = busy; }
  });
  row.querySelector(".alias-example").textContent = `sn @${root.alias || "别名"} 关键词`;
  row.querySelector(".remove-root").setAttribute("aria-label", `移除${root.alias || "新"}目录`);
  return row;
}
function render(data) {
  if (doctorReport && serverData && JSON.stringify(serverData.config) !== JSON.stringify(data.config)) {
    resetDoctor(); $("#doctor-state").textContent = "需要重新检查";
    $("#doctor-status").textContent = "已保存配置发生变化，请重新运行检查并确认操作。";
  }
  serverData = data;
  const config = data.config;
  saved = {roots: config.roots.map(root => ({alias: root.alias, path: root.path, exclude_dirs: root.exclude_dirs || []})), max_results: config.max_results ?? 30, max_file_bytes: config.max_file_bytes ?? 2000000, editor: config.editor ?? "default"};
  $("#roots").replaceChildren(...saved.roots.map(root => rootRow(root, data.root_states.find(state => state.alias === root.alias)?.exists ?? false)));
  $("#max-results").value = saved.max_results; $("#max-size").value = saved.max_file_bytes / 1000000;
  $("#editor").value = saved.editor;
  $("#editor-note").textContent = data.editors?.vscode ? "已检测到 VS Code。选择后，Flow 回车和阅读面板可定位到命中行。" : "未检测到 VS Code。系统默认程序仍可打开文件；VS Code 定位需要本机已安装。";
  $("#root-total").textContent = String(config.roots.length); $("#nav-count").textContent = String(config.roots.length);
  $("#file-total").textContent = String(data.stats.files); $("#paragraph-total").textContent = String(data.stats.paragraphs);
  $("#version").textContent = `ParagraphBox · v${data.version}`;
  $("#summary-note").textContent = data.sample_mode ? "当前使用人工样例，可随时添加自己的目录。" : "下一次搜索将自动更新资料索引。";
  renderScopes(data);
  renderIndex(data); showIssues(); updateDirty();
}

function renderIndex(data) {
  if (!data) return;
  const status = data.index_status || {}, report = status.report;
  $("#index-state").textContent = !report ? "尚未建立" : !status.config_current ? "待更新" : report.issue_count ? "有跳过项" : "已更新";
  $("#index-note").textContent = !report ? "搜索一次，或点击重新建立索引。" : !status.config_current ? "资料目录或规则已更改，下次搜索会立即更新索引。" : `上次扫描更新 ${report.updated} 个文件、移除 ${report.deleted} 个文件${report.issue_count ? "；部分资料未能索引。" : "。"}`;
  $("#index-time").textContent = report ? new Date(report.finished_at).toLocaleString("zh-CN", {hour12: false}) : "—";
  $("#index-duration").textContent = report ? `${report.duration_ms} ms` : "—";
  $("#index-skipped").textContent = report ? `${report.skipped} 个` : "—";
  $("#index-excluded").textContent = report ? `${report.excluded} 处入口` : "—";
  const issueItems = (report?.issues || []).map(issue => {
    const item = document.createElement("li"), message = document.createElement("strong"), path = document.createElement("span");
    message.textContent = issue.message; path.textContent = issue.path; item.append(message, path); return item;
  });
  $("#index-issue-list").replaceChildren(...issueItems);
  $("#index-issues").hidden = !report?.issue_count;
  $("#index-issues-summary").textContent = report ? `查看 ${report.issue_count} 项原因${report.issue_count > 50 ? "（显示前 50 项）" : ""}` : "查看未能索引的原因";
  $("#index-timings").hidden = !report?.timings_ms;
  const timings = report?.timings_ms || {};
  const labels = {scan: "目录检查", read: "文件读取", parse: "段落整理", database: "数据库操作", lock: "等待更新", other: "其他处理"};
  $("#index-timing-list").replaceChildren(...Object.entries(labels).map(([key, label]) => {
    const item = document.createElement("div"), name = document.createElement("dt"), value = document.createElement("dd");
    name.textContent = label; value.textContent = `${Number(timings[key] || 0).toFixed(1)} ms`; item.append(name, value); return item;
  }));
}
$("#rebuild-index").addEventListener("click", async () => {
  if (busy || isDirty()) return;
  indexBusy = true; setSaving(true); notice(); resetPreview("正在重新建立索引，完成后可重新试搜。");
  $("#rebuild-index").textContent = "正在重建…"; $("#index-state").textContent = "正在重建";
  $("#index-note").textContent = "正在读取已保存目录中的文件，请稍候。";
  document.querySelectorAll("#preview-form input, #preview-form button, [data-query]").forEach(field => { field.disabled = true; });
  const operation = crypto.randomUUID();
  const stopProgress = watchIndex(operation, () => indexBusy);
  try { const data = await api("/api/rebuild-index", {}, undefined, operation); stopProgress(); render(data); resetPreview("索引已更新，输入关键词重新试搜。"); toast(`索引已重新建立，更新 ${data.index_status.report.updated} 个文件。`); }
  catch (error) { renderIndex(serverData); resetPreview("重建未完成，可重试或重新检索。"); notice(`重建未完成：${error.message}`); }
  finally { stopProgress(); indexBusy = false; setSaving(false); $("#rebuild-index").textContent = "重新建立索引"; document.querySelectorAll("#preview-form input, #preview-form button, [data-query]").forEach(field => { field.disabled = false; }); syncScopes(); }
});
$("#add-root").addEventListener("click", () => {
  const row = rootRow({alias: "", path: ""}, false); row.dataset.state = "pending"; rowState(row, "pending");
  $("#roots").append(row); showIssues(); notice(); updateDirty(); row.querySelector(".alias-input").focus();
});
for (const field of ["#max-results", "#max-size", "#editor"]) $(field).addEventListener("input", () => { showIssues(); notice(); updateDirty(); });
$("#revert").addEventListener("click", () => { render(serverData); notice(); });
$("#save").addEventListener("click", async () => {
  if (!isDirty() || busy) return;
  const data = formDocument();
  if (!data.roots.length) { notice("请至少添加一个资料目录。"); return; }
  setSaving(true); notice(); showIssues(); $("#save").lastChild.textContent = "正在保存…";
  let invalidField = null;
  try { render(await api("/api/config", data)); resetPreview("设置已更新，输入关键词重新试搜。"); toast("设置已保存，下一次搜索即可生效。"); }
  catch (error) { invalidField = showIssues(error.issues); notice(invalidField ? "设置未保存，请修正标出的字段。原有配置仍然保留。" : error.message); }
  finally { setSaving(false); $("#save").lastChild.textContent = "保存更改"; invalidField?.focus(); }
});

function highlighted(element, text, ranges = []) {
  const chars = Array.from(text); let cursor = 0;
  element.replaceChildren();
  for (const [start, end] of ranges) {
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end <= start || end > chars.length) continue;
    element.append(document.createTextNode(chars.slice(cursor, start).join("")));
    const mark = document.createElement("mark"); mark.textContent = chars.slice(start, end).join(""); element.append(mark); cursor = end;
  }
  element.append(document.createTextNode(chars.slice(cursor).join("")));
}
function renderSearchTiming(status) {
  $("#search-timings").hidden = !status?.search;
  if (!status?.search) return;
  $("#search-timing-summary").textContent = `本次检索 ${status.duration_ms.toFixed(1)} ms · ${status.index_reused ? "复用近期索引" : "已检查文件变化"}`;
  const stages = [["准备资料", status.prepare_ms], ["索引检查", status.index_ms], ["资料匹配", status.search.query_ms],
                  ["结果排序", status.search.ranking_ms], ["范围统计", status.scope_ms || 0], ["结果整理", status.search.result_ms + status.format_ms], ["完成处理", status.close_ms]];
  $("#search-timing-list").replaceChildren(...stages.map(([label, duration]) => {
    const item = document.createElement("div"), name = document.createElement("dt"), value = document.createElement("dd");
    name.textContent = label; value.textContent = `${duration.toFixed(1)} ms`; item.append(name, value); return item;
  }));
  $("#search-matched").textContent = `匹配 ${status.search.matched} 个段落，返回 ${status.search.returned} 条；计时不含启动插件进程和页面显示。`;
}
function contextBlock(data, label, selected = false, original = null) {
  const section = document.createElement("section"); section.className = `context-block${selected ? " selected" : ""}`;
  const name = document.createElement("strong"); name.className = "context-label"; name.textContent = label; section.append(name);
  if (!data) {
    const boundary = document.createElement("p"); boundary.className = "context-boundary";
    boundary.textContent = label === "前一段" ? "已到文件开头，没有前一段。" : "已到文件结尾，没有后一段。";
    section.append(boundary); return section;
  }
  const body = document.createElement("p"); body.className = "context-text";
  highlighted(body, data.text, selected ? original?.text_matches : []);
  const source = document.createElement("small");
  const lines = data.line_start === data.line_end ? data.line_start : `${data.line_start}–${data.line_end}`;
  source.textContent = `${selected ? `${data.path.split(/[\\/]/).pop()} · ` : ""}第 ${lines} 行${data.heading ? ` · ${data.heading}` : ""}`;
  section.append(body, source); return section;
}
async function paragraphHash(context) {
  const bytes = new TextEncoder().encode(`${context.heading}\0${context.text}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}
async function locationRequest(context) {
  return {path: context.path, alias: context.alias, line_start: context.line_start,
          line_end: context.line_end, text_hash: await paragraphHash(context)};
}
function citation(context) {
  const line = context.line_start === context.line_end ? context.line_start : `${context.line_start}–${context.line_end}`;
  return `${context.text}\n\n出处：${context.path}${context.heading ? ` · ${context.heading}` : ""} · 第 ${line} 行`;
}
function closeReader() {
  if (!$("#reader").open) return;
  ++readerSession; readerController?.abort(); readerContext = null;
  $("#reader").close();
}
function readerStatus(message = "", error = false) {
  const status = $("#reader-status"); status.textContent = message; status.hidden = !message;
  status.className = `reader-status${error ? " error" : ""}`; status.setAttribute("role", error ? "alert" : "status");
}
async function loadReaderContext(context, session) {
  readerController?.abort(); readerController = new AbortController();
  const signal = readerController.signal;
  readerContext = null; readerActionBusy = false; updateReaderActions();
  $("#reader-retry").hidden = true; $("#reader-search").hidden = true;
  readerStatus("正在检查资料并读取前后文…");
  $("#reader-content").replaceChildren();
  try {
    const data = await api("/api/context", await locationRequest(context), signal);
    if (session !== readerSession || !$("#reader").open) return;
    readerContext = data.selected;
    $("#reader-content").replaceChildren(contextBlock(data.before, "前一段"), contextBlock(data.selected, "命中段落", true, context), contextBlock(data.after, "后一段"));
    readerStatus(); updateReaderActions();
  } catch (error) {
    if (error.name === "AbortError" || session !== readerSession || !$("#reader").open) return;
    readerStatus(error.message, true); $("#reader-retry").hidden = false; $("#reader-search").hidden = false;
  }
}
function updateReaderActions() {
  $("#reader-open").disabled = $("#reader-copy").disabled = !readerContext || readerActionBusy;
}
function selectReader(position) {
  if (!Number.isInteger(position) || position < 0 || position >= readerResults.length || !$("#reader").open) return;
  const session = ++readerSession;
  readerPosition = position;
  const context = readerResults[position]; readerOriginal = context;
  $("#reader-position").textContent = `第 ${position + 1} / ${readerResults.length} 条`;
  $("#reader-query").textContent = `本次检索：${readerQuery || "关键词"} · 按返回顺序浏览`;
  $("#reader-previous").disabled = position === 0;
  $("#reader-next").disabled = position === readerResults.length - 1;
  if (document.activeElement?.disabled && [$("#reader-previous"), $("#reader-next")].includes(document.activeElement)) {
    (position === 0 && readerResults.length > 1 ? $("#reader-next") : position > 0 ? $("#reader-previous") : $("#reader-close")).focus();
  }
  $("#reader-title").textContent = context.path.split(/[\\/]/).pop();
  $("#reader-meta").textContent = `${context.alias}${context.heading ? ` · ${context.heading}` : ""} · 第 ${context.line_start === context.line_end ? context.line_start : `${context.line_start}–${context.line_end}`} 行`;
  $("#reader-path").textContent = context.path; $("#reader-location").open = false;
  $("#reader-open").textContent = serverData.config.editor === "vscode" ? "定位到命中行" : "打开原文件";
  $("#reader .reader-body").scrollTop = 0;
  loadReaderContext(context, session);
}
function openReader(context, opener, position) {
  readerOpener = opener;
  readerResults = previewHits.map(hit => hit.ContextData);
  if (readerResults[position] !== context) { readerResults = [context]; position = 0; }
  readerQuery = previewQuery;
  $("#reader").showModal(); document.body.classList.add("reader-open");
  selectReader(position);
}
function moveReader(step) { selectReader(readerPosition + step); }
$("#reader-previous").addEventListener("click", () => moveReader(-1));
$("#reader-next").addEventListener("click", () => moveReader(1));
$("#reader").addEventListener("keydown", event => {
  // Plain arrows retain text selection, scrolling and native control behavior.
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
  if (event.target.closest("input,textarea,select,[contenteditable]:not([contenteditable=false])")) return;
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault(); moveReader(event.key === "ArrowUp" ? -1 : 1);
  }
});
$("#reader-close").addEventListener("click", closeReader);
$("#reader").addEventListener("close", () => {
  if ($("#reader").open) return;
  ++readerSession; readerController?.abort(); readerContext = null; readerOriginal = null;
  readerResults = []; readerPosition = -1; readerQuery = ""; readerActionBusy = false;
  document.body.classList.remove("reader-open");
  if (readerOpener?.isConnected) readerOpener.focus(); readerOpener = null;
});
$("#reader").addEventListener("click", event => {
  if (event.target !== $("#reader")) return;
  const rect = $("#reader").getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeReader();
});
$("#reader-retry").addEventListener("click", () => { if (readerOriginal) selectReader(readerPosition); });
$("#reader-search").addEventListener("click", () => { const query = readerQuery; closeReader(); $("#preview-query").value = query; search(query); $("#preview-query").focus(); });
$("#reader-open").addEventListener("click", async () => {
  if (!readerContext || readerActionBusy) return;
  const session = readerSession, context = readerContext; readerActionBusy = true; updateReaderActions();
  try {
    const location = await locationRequest(context);
    if (session !== readerSession || !$("#reader").open) return;
    const result = await api("/api/open-location", location);
    if (session === readerSession && $("#reader").open) readerStatus(result.located ? `已请求在 VS Code 中打开第 ${context.line_start} 行。` : "已请求打开原文件。");
  } catch (error) {
    if (session === readerSession && $("#reader").open) {
      readerContext = null; $("#reader-content").replaceChildren(); readerStatus(error.message, true);
      $("#reader-retry").hidden = false; $("#reader-search").hidden = false;
    }
  } finally { if (session === readerSession) { readerActionBusy = false; updateReaderActions(); } }
});
$("#reader-copy").addEventListener("click", async () => {
  if (!readerContext || readerActionBusy) return;
  const session = readerSession, context = readerContext, signal = readerController.signal;
  readerActionBusy = true; updateReaderActions(); readerStatus("正在核对原文，准备复制…");
  try {
    const verified = await api("/api/context", await locationRequest(context), signal);
    if (session !== readerSession || !$("#reader").open || signal.aborted) return;
    try {
      await navigator.clipboard.writeText(citation(verified.selected));
      if (session === readerSession && $("#reader").open) readerStatus("摘录已复制，包含当前命中段落与出处。");
    } catch {
      if (session === readerSession && $("#reader").open) readerStatus("浏览器暂时无法复制，请在 Flow 的结果菜单中复制摘录。", true);
    }
  } catch (error) {
    if (error.name !== "AbortError" && session === readerSession && $("#reader").open) {
      readerContext = null; $("#reader-content").replaceChildren(); readerStatus(error.message, true);
      $("#reader-retry").hidden = false; $("#reader-search").hidden = false;
    }
  } finally { if (session === readerSession) { readerActionBusy = false; updateReaderActions(); } }
});
function hitCard(result, position) {
  const context = result.ContextData;
  const card = document.createElement("article"); card.className = "preview-hit";
  const tag = document.createElement("span"); tag.className = "hit-tag"; tag.textContent = context.alias;
  const kind = document.createElement("span"); kind.className = "match-label";
  kind.textContent = {body: "正文命中", heading: "章节标题命中", mixed: "正文 + 章节命中"}[context.match_kind] || "匹配段落";
  const text = document.createElement("p"); text.className = "hit-title"; highlighted(text, result.Title, context.snippet_matches);
  const line = context.line_start === context.line_end ? String(context.line_start) : `${context.line_start}–${context.line_end}`;
  const source = document.createElement("div"); source.className = "hit-source";
  source.append(document.createTextNode(`${context.path.split(/[\\/]/).pop()} · 第 ${line} 行`));
  if (context.heading) { const heading = document.createElement("span"); heading.className = "hit-heading"; highlighted(heading, context.heading, context.heading_matches); source.append(heading); }
  const actions = document.createElement("div"); actions.className = "hit-actions";
  const location = document.createElement("details"), locationSummary = document.createElement("summary"), fullPath = document.createElement("p");
  location.className = "hit-location"; locationSummary.textContent = "查看完整路径"; fullPath.textContent = context.path; location.append(locationSummary, fullPath);
  const open = document.createElement("button"); open.type = "button"; open.className = "open-file"; open.textContent = "打开原文件";
  open.addEventListener("click", async () => { open.disabled = true; try { await api("/api/open-file", {path: context.path}); toast("已请求打开原文件。"); } catch (error) { notice(error.message); } finally { open.disabled = false; } });
  const read = document.createElement("button"); read.type = "button"; read.className = "show-context";
  read.textContent = "阅读段落"; read.setAttribute("aria-haspopup", "dialog");
  read.addEventListener("click", () => openReader(context, read, position));
  const copy = document.createElement("button"); copy.type = "button"; copy.className = "copy-excerpt"; copy.textContent = "复制带出处的摘录";
  copy.addEventListener("click", async () => {
    const number = searchNumber; copy.disabled = true;
    try {
      const data = await api("/api/context", await locationRequest(context));
      if (number !== searchNumber || !card.isConnected) return;
      try { await navigator.clipboard.writeText(citation(data.selected)); toast("摘录已复制，出处也一起保留了。"); }
      catch { notice("浏览器暂时无法复制，请在 Flow 的结果菜单中复制摘录。"); }
    } catch (error) { if (number === searchNumber && card.isConnected) notice(error.message); }
    finally { copy.disabled = false; }
  });
  actions.append(read, open, copy); card.append(tag, kind, text, source, location, actions); return card;
}
function renderSearchCount(summary, hits) {
  const count = $("#preview-count"), limit = $("#search-limit-note");
  limit.hidden = true;
  if (!Number.isInteger(summary?.matched)) {
    count.textContent = summary ? "先确认搜索范围与关键词，再开始检索。" : `本次返回 ${hits.length} 条段落，卡片预览前 4 条。`;
    return;
  }
  count.textContent = `${summary.scope === null ? "全部目录" : `@${summary.scope}`} · 匹配 ${summary.matched} 条，返回 ${summary.returned} 条。${hits.length > 4 ? "卡片预览前 4 条，阅读面板可浏览全部返回结果。" : ""}`;
  limit.hidden = summary.matched <= summary.returned;
  $("#search-limit-text").textContent = `当前返回上限为 ${summary.limit} 条。选择更具体的目录或补充关键词，可缩小匹配范围。`;
}
function emptySearch(summary) {
  const section = document.createElement("section"); section.className = "search-empty";
  const title = document.createElement("h3"), text = document.createElement("p");
  const reason = summary.empty_reason;
  title.textContent = {unknown_alias: "这个目录别名尚未配置", keywords_required: "输入关键词开始检索", directories_unavailable: "当前范围的目录不可用", no_paragraphs: "当前范围还没有可检索段落", no_match: "没有找到匹配段落"}[reason] || "没有找到匹配段落";
  text.textContent = {unknown_alias: `未识别 @${summary.requested_alias}。请点击上方已保存的目录，或检查目录别名。`,
    keywords_required: "选择范围后，再输入你记得的一个词。首次搜索会自动建立索引。",
    directories_unavailable: "请检查已保存目录是否被移动、删除或无法访问，修复后再重试。",
    no_paragraphs: `当前范围已索引 ${summary.scope_files} 个文件、0 个段落。确认目录中有 Markdown / TXT 正文${summary.has_exclusions ? "，并检查排除规则" : ""}。`,
    no_match: `当前范围已有 ${summary.scope_paragraphs} 个可检索段落。${summary.keywords.split(/\s+/).length > 1 ? "多个词需全部命中同一段落或章节，可先减少关键词。" : "试试更短的关键词，或扩大搜索范围。"}`}[reason] || "调整关键词后再试一次。";
  const actions = document.createElement("div"); actions.className = "empty-actions";
  const button = (label, handler) => { const item = document.createElement("button"); item.type = "button"; item.className = "button secondary"; item.textContent = label; item.addEventListener("click", handler); actions.append(item); };
  const link = (label, target) => { const item = document.createElement("a"); item.className = "button secondary"; item.textContent = label; item.href = target; actions.append(item); };
  if (reason === "keywords_required") button("输入关键词", () => $("#preview-query").focus());
  else if (reason === "no_match") {
    button("调整关键词", () => $("#preview-query").focus());
    if (summary.keywords.split(/\s+/).length > 1) button("只搜第一个词", () => { $("#preview-query").value = scopeQuery(summary.scope, summary.keywords.split(/\s+/)[0]); search($("#preview-query").value); });
    if (summary.scope !== null) button("搜索全部目录", () => { $("#preview-query").value = summary.keywords; syncScopes(); search(summary.keywords); });
  } else link("检查资料目录", "#directories");
  if (reason === "directories_unavailable" || reason === "no_paragraphs") button("重试检索", () => search($("#preview-query").value));
  section.append(title, text);
  if (summary.unavailable_roots && reason !== "directories_unavailable") {
    const warning = document.createElement("p"); warning.textContent = `当前范围有 ${summary.unavailable_roots} 个目录不可用，可能影响结果。`; section.append(warning); link("检查资料目录", "#directories");
  }
  if (summary.index_issue_count) {
    const warning = document.createElement("p"); warning.className = "empty-warning";
    warning.textContent = `所有目录的本次索引有 ${summary.index_issue_count} 项问题，跳过 ${summary.skipped_total} 个文件；可能与当前范围不同。`;
    section.append(warning);
    button("查看索引原因", () => { $("#index-issues").hidden = false; $("#index-issues").open = true; $("#index-issues").scrollIntoView({block:"center"}); $("#index-issues summary").focus(); });
  }
  section.append(actions); return section;
}
function resetPreview(message) {
  closeReader();
  previewHits = []; previewQuery = "";
  ++searchNumber; searchController?.abort();
  stopSearchProgress?.(); stopSearchProgress = null;
  if (!indexBusy && serverData) renderIndex(serverData);
  renderSearchTiming(null);
  $("#search-limit-note").hidden = true;
  $("#preview-search").setAttribute("aria-busy", "false");
  $("#preview-count").textContent = "卡片预览前 4 条；阅读面板可连续浏览本次返回的全部结果。";
  const placeholder = document.createElement("div"); placeholder.className = "preview-message"; placeholder.textContent = message;
  $("#preview-results").replaceChildren(placeholder);
}
async function search(query) {
  if (indexBusy) return;
  closeReader();
  previewHits = []; previewQuery = "";
  syncScopes(); $("#search-limit-note").hidden = true;
  if (!query.trim()) { resetPreview("输入关键词，找回你记得的那一段。"); $("#preview-query").focus(); return; }
  const number = ++searchNumber;
  stopSearchProgress?.();
  searchController?.abort(); searchController = new AbortController(); const signal = searchController.signal;
  const resultsElement = $("#preview-results"); resultsElement.replaceChildren();
  renderSearchTiming(null);
  $("#preview-search").setAttribute("aria-busy", "true"); $("#preview-count").textContent = "正在查找最新关键词…";
  const loading = document.createElement("div"); loading.className = "preview-message"; loading.textContent = "正在资料中查找…"; resultsElement.append(loading);
  const operation = crypto.randomUUID();
  const stopProgress = watchIndex(operation, () => number === searchNumber && !indexBusy, true);
  stopSearchProgress = stopProgress;
  try {
    const data = await api("/api/search", {query: query.trim().replace(/^sn\s+/, "")}, signal, operation);
    stopProgress();
    if (number !== searchNumber) return;
    renderSearchTiming(data.search_status);
    const hits = data.results.filter(result => result.ContextData);
    previewHits = hits; previewQuery = query.trim().replace(/^sn\s+/, "");
    resultsElement.replaceChildren(...hits.slice(0, 4).map(hitCard));
    const summary = data.search_status?.summary;
    renderSearchCount(summary, hits);
    if (summary && samePreviewQuery(query)) syncScopes(summary.empty_reason === "unknown_alias" ? undefined : summary.scope);
    if (!hits.length && summary) resultsElement.append(emptySearch(summary));
    for (const result of data.results.filter(result => !result.ContextData && (hits.length || !summary))) {
      const message = document.createElement("div"); message.className = "preview-message"; message.textContent = `${result.Title}：${result.SubTitle}`; resultsElement.append(message);
    }
    if (isDirty()) toast("试搜使用的是已保存目录；保存后再试搜新目录。");
    const updated = await api("/api/config", undefined, signal);
    if (number !== searchNumber) return;
    $("#file-total").textContent = String(updated.stats.files); $("#paragraph-total").textContent = String(updated.stats.paragraphs);
    serverData = updated; renderIndex(updated); renderScopes(updated, summary && samePreviewQuery(query) && summary.empty_reason !== "unknown_alias" ? summary.scope : undefined);
    if (!hits.length && summary?.index_issue_count) $("#index-issues").hidden = false;
  } catch (error) {
    if (number !== searchNumber || error.name === "AbortError") return;
    closeReader(); previewHits = []; previewQuery = "";
    $("#search-limit-note").hidden = true; renderSearchTiming(null);
    resultsElement.replaceChildren(); const message = document.createElement("div"); message.className = "preview-message";
    message.textContent = error.message; resultsElement.append(message); $("#preview-count").textContent = "本次检索未完成，可重试或从 Flow 重新打开设置。";
    renderIndex(serverData);
  } finally { stopProgress(); if (number === searchNumber) { stopSearchProgress = null; $("#preview-search").setAttribute("aria-busy", "false"); } }
}
function useExample(keywords) {
  $("#preview-query").value = scopeQuery(queryParts($("#preview-query").value).alias, keywords);
  search($("#preview-query").value);
}
$("#preview-form").addEventListener("submit", event => { event.preventDefault(); search($("#preview-query").value); });
document.querySelectorAll("[data-query]").forEach(button => button.addEventListener("click", () => useExample(button.dataset.query)));
$("#open-config-file").addEventListener("click", async () => { try { await api("/api/open-config-file", {}); } catch (error) { notice(error.message); } });
function updateNavigation() { const hash = location.hash || "#directories"; document.querySelectorAll(".nav-item").forEach(link => link.classList.toggle("active", link.getAttribute("href") === hash)); }
window.addEventListener("hashchange", updateNavigation); updateNavigation();
window.addEventListener("beforeunload", event => { if (isDirty()) { event.preventDefault(); event.returnValue = ""; } });
api("/api/config").then(render).catch(() => {
  notice("资料配置无法加载。请运行检查，再从页尾“编辑配置文件”修复并重新打开设置。");
  $("#summary-note").textContent = "配置暂不可用；运行检查和人工样例仍可使用。";
  $("#index-state").textContent = "无法读取"; $("#index-note").textContent = "修复资料配置后，重新打开设置查看索引状态。";
  document.querySelectorAll("#add-root, #preferences input, #preferences select, #preview-form input, #preview-form button, [data-query]").forEach(field => { field.disabled = true; });
  updateDirty();
});

let doctorReport = null;
let doctorSummary = null;
let demoData = null;
let doctorNumber = 0;
const manualIds = ["open_file", "paste_excerpt", "flow_entry"];
function resetDoctor() {
  ++doctorNumber;
  doctorReport = doctorSummary = demoData = null;
  $("#doctor-checks").replaceChildren(); $("#doctor-summary").value = "";
  $("#doctor-copy").disabled = true;
  $("#doctor-run").disabled = $("#demo-search").disabled = false;
  $("#demo-result").hidden = true;
  $("#demo-citation").hidden = true; $("#demo-citation").value = "";
  $("#demo-open").disabled = $("#demo-copy").disabled = true;
  manualIds.forEach(id => { const field = $(`#manual-${id}`); field.checked = false; field.disabled = true; });
}
function updateDoctorSummary() {
  if (!doctorSummary) return;
  const document = structuredClone(doctorSummary);
  manualIds.forEach(id => { document.manual[id] = $(`#manual-${id}`).checked ? "user_confirmed" : "pending"; });
  $("#doctor-summary").value = JSON.stringify(document, null, 2);
  const count = manualIds.filter(id => $(`#manual-${id}`).checked).length;
  $("#doctor-state").textContent = doctorReport.automatic_status === "fail" ? "自动检查发现问题" : "自动检查通过";
  $("#doctor-status").textContent = `手动确认 ${count}/3 项。仅勾选你实际完成的操作；确认只保留在当前页面。`;
}
async function runDoctor() {
  resetDoctor(); $("#doctor-run").disabled = true; $("#demo-search").disabled = true;
  const number = doctorNumber;
  $("#doctor-state").textContent = "正在检查";
  $("#doctor-status").textContent = "检查环境与人工样例，不检索你的真实资料。";
  try {
    const data = await api("/api/diagnostics", {});
    if (number !== doctorNumber) return;
    doctorReport = data.report; doctorSummary = JSON.parse(data.summary);
    $("#version").textContent = `ParagraphBox · v${doctorReport.version}`;
    $("#doctor-checks").replaceChildren(...doctorReport.checks.map(check => {
      const row = document.createElement("li"), heading = document.createElement("div"), label = document.createElement("strong"), state = document.createElement("span"), advice = document.createElement("p");
      row.className = `doctor-check ${check.status}`;
      label.textContent = check.label; state.textContent = {pass: "通过", fail: "需处理", pending: "待确认"}[check.status];
      heading.append(label, state); advice.textContent = [check.hint, check.code !== "OK" ? `代码：${check.code}` : ""].filter(Boolean).join(" ");
      row.append(heading); if (advice.textContent) row.append(advice); return row;
    }));
    $("#manual-flow_entry").disabled = false;
    $("#doctor-copy").disabled = false;
    updateDoctorSummary();
  } catch {
    if (number !== doctorNumber) return;
    $("#doctor-state").textContent = "检查未完成";
    $("#doctor-status").textContent = "请从 Flow 重新打开设置，或在插件目录运行 python main.py --doctor。";
  } finally { if (number === doctorNumber) { $("#doctor-run").disabled = false; $("#demo-search").disabled = false; } }
}
$("#doctor-run").addEventListener("click", runDoctor);
manualIds.forEach(id => $(`#manual-${id}`).addEventListener("change", updateDoctorSummary));
$("#demo-search").addEventListener("click", async () => {
  $("#demo-search").disabled = true; demoData = null;
  $("#demo-result").hidden = true;
  $("#demo-citation").hidden = true; $("#demo-citation").value = "";
  $("#demo-open").disabled = $("#demo-copy").disabled = true;
  for (const id of ["open_file", "paste_excerpt"]) { $(`#manual-${id}`).checked = false; $(`#manual-${id}`).disabled = true; }
  updateDoctorSummary(); $("#demo-status").textContent = "正在检索独立人工样例…";
  try {
    demoData = await api("/api/demo", {});
    $("#demo-text").textContent = demoData.text; $("#demo-source").textContent = demoData.source;
    $("#demo-result").hidden = false; $("#demo-open").disabled = $("#demo-copy").disabled = false;
    $("#demo-status").textContent = `“${demoData.query}”命中 1 段，行号为 3。接下来验证打开与粘贴。`;
  } catch { $("#demo-status").textContent = "人工样例检索失败，请重新运行检查。"; }
  finally { $("#demo-search").disabled = false; }
});
$("#demo-open").addEventListener("click", async () => {
  $("#demo-open").disabled = true;
  try {
    await api("/api/demo-open", {});
    $("#demo-status").textContent = "已请求打开人工样例。看到编辑器里的原文后，再勾选下方确认。";
    if (doctorSummary) $("#manual-open_file").disabled = false;
  } catch { $("#demo-status").textContent = "未能请求打开样例。检查文件关联或重新安装插件。"; }
  finally { $("#demo-open").disabled = !demoData; }
});
$("#demo-copy").addEventListener("click", async () => {
  if (!demoData) return;
  try {
    await navigator.clipboard.writeText(demoData.citation);
    $("#demo-status").textContent = "已复制人工摘录。请粘贴到记事本，核对正文、出处和第 3 行，再勾选确认。";
  } catch { $("#demo-status").textContent = "浏览器未允许复制。可选中下方人工摘录手动复制并粘贴核对。"; }
  if (doctorSummary) $("#manual-paste_excerpt").disabled = false;
  $("#demo-citation").value = demoData.citation; $("#demo-citation").hidden = false;
});
$("#doctor-copy").addEventListener("click", async () => {
  updateDoctorSummary();
  try { await navigator.clipboard.writeText($("#doctor-summary").value); toast("已复制脱敏诊断摘要。"); }
  catch { $("#doctor-details").open = true; $("#doctor-summary").focus(); $("#doctor-summary").select(); toast("请按 Ctrl+C 复制已选中的脱敏摘要。"); }
});
if (requestedView === "diagnostics") { location.hash = "diagnostics"; runDoctor(); }
