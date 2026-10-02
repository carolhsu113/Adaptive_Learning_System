"use strict";

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "als.active_session_id";
const fields = ["concern", "problem", "original_answer", "attempts", "other_known_information"];
const fieldLabels = {concern: "当前困惑", problem: "题目", original_answer: "原答案", attempts: "已尝试步骤", other_known_information: "其他已知信息"};
let view = null;
let busy = false;
let draftDirty = false;
let draftTimer = null;
let draftSerial = Promise.resolve(true);
let currentTurn = null;
let recoveryProblem = false;
let generation = 0;
let pendingTimer = null;

function requestId() { return crypto.randomUUID(); }
async function request(method, path, payload) {
  try {
    const response = await fetch(path, {
      method,
      headers: payload === undefined ? undefined : {"Content-Type": "application/json"},
      body: payload === undefined ? undefined : JSON.stringify(payload),
      cache: "no-store"
    });
    return {ok: response.ok, status: response.status, body: await response.json()};
  } catch (_) {
    return {ok: false, status: 503, body: null};
  }
}
function remember(next) {
  view = next;
  if (next && next.session_id && next.state !== "completed") localStorage.setItem(STORAGE_KEY, next.session_id);
  if (next && next.state === "completed") localStorage.removeItem(STORAGE_KEY);
}
function errorText(result, fallback) { return result.body?.error?.message || fallback; }
function taskInput() {
  return Object.fromEntries(fields.map((field) => [field, $("start-form").elements[field].value]));
}
function hasTask(input) { return Object.values(input).some((value) => value.trim()); }
function setSaveStatus(message, failed = false) {
  const answerMode = view && ["final_answer", "submit_failed"].includes(view.state);
  const target = answerMode ? $("answer-save-status") : $("task-save-status");
  target.textContent = message;
  $(answerMode ? "retry-answer-save" : "retry-task-save").hidden = !failed;
}
function queueDraft() {
  draftDirty = true;
  setSaveStatus("保存中…");
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => { void flushDraft(); }, 300);
}
function flushDraft() {
  clearTimeout(draftTimer);
  draftTimer = null;
  if (!draftDirty) return draftSerial;
  const epoch = generation;
  draftDirty = false;
  const answerMode = view && ["final_answer", "submit_failed"].includes(view.state);
  const payload = answerMode ? {final_answer_draft: $("answer").value} : {task_input: taskInput()};
  draftSerial = draftSerial.then(async () => {
    if (!view) {
      const created = await request("POST", "/api/sessions", {request_id: requestId()});
      if (epoch !== generation) return false;
      if (!created.ok) { draftDirty = true; setSaveStatus(errorText(created, "未能创建会话，请重试保存。"), true); return false; }
      remember(created.body);
    }
    const saved = await request("PUT", `/api/sessions/${view.session_id}/draft`, {
      request_id: requestId(), expected_revision: view.revision, ...payload
    });
    if (epoch !== generation) return false;
    if (!saved.ok) {
      if (saved.status === 409) {
        const latest = await request("GET", `/api/sessions/${view.session_id}`);
        if (epoch !== generation) return false;
        if (latest.ok && latest.body?.state === view.state) remember(latest.body);
      }
      draftDirty = true;
      setSaveStatus(errorText(saved, "保存失败，请重新保存。"), true);
      return false;
    }
    remember(saved.body);
    if (!draftDirty) setSaveStatus("草稿已保存");
    return true;
  });
  return draftSerial;
}
function textElement(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}
function renderMaterials() {
  const list = $("material-list");
  const card = $("task-card");
  list.replaceChildren();
  card.replaceChildren(textElement("strong", "", "你提供的材料"));
  if (!view) return;
  for (const field of fields) {
    const value = view.task_input[field];
    if (!value) continue;
    const item = textElement("div", "material", value);
    item.prepend(textElement("b", "", fieldLabels[field]));
    list.append(item);
    card.append(textElement("div", "", `${fieldLabels[field]}：${value}`));
  }
}
function renderMessages() {
  const stream = $("stream");
  const follow = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
  const box = $("messages");
  box.replaceChildren();
  for (const message of view?.conversation || []) {
    const student = message.role === "student";
    const row = textElement("div", `message-row ${student ? "student" : "assistant"}`, "");
    const body = textElement("div", "message-body", "");
    const speaker = textElement("div", "speaker", "");
    if (student) {
      speaker.append(textElement("span", "student-avatar", "林"));
      speaker.append(textElement("span", "", "小林"));
    } else speaker.textContent = "Socratic";
    body.append(speaker, textElement("div", "bubble", message.text));
    row.append(body);
    box.append(row);
  }
  if (follow) stream.scrollTop = stream.scrollHeight;
}
function render() {
  const state = view?.state;
  const runtimeLocked = view?.error?.code === "runtime_lock_changed";
  $("start-form").hidden = (!!state && state !== "help_input") || recoveryProblem;
  $("dialogue").hidden = !state || state === "help_input" || recoveryProblem;
  $("materials").hidden = !state || state === "help_input" || state === "completed" || recoveryProblem;
  $("new-problem").hidden = !state && !recoveryProblem;
  $("recovery-card").hidden = !recoveryProblem;
  $("chat-composer").hidden = state !== "tutoring" || recoveryProblem || view?.error?.code === "boundary_stop" || runtimeLocked;
  $("answer-composer").hidden = !["final_answer", "submitting", "submit_failed"].includes(state) || recoveryProblem;
  $("ready-card").hidden = state !== "ready_for_submission";
  $("completed-card").hidden = state !== "completed";
  $("reply").disabled = busy || runtimeLocked || view?.operation_status === "pending" || (!!currentTurn && !!view?.error?.retryable);
  $("send").disabled = $("reply").disabled;
  $("answer").disabled = busy || runtimeLocked || state === "submitting";
  $("submit-answer").disabled = $("answer").disabled;
  $("enter-answer").disabled = runtimeLocked;
  for (const input of $("start-form").querySelectorAll("textarea, button")) input.disabled = runtimeLocked;
  $("submit-answer").textContent = state === "submit_failed" ? "重新提交" : state === "submitting" ? "提交中…" : "提交作答";
  if (state === "help_input") {
    for (const field of fields) $("start-form").elements[field].value = view.task_input[field] || "";
    $("task-save-status").textContent = "草稿已保存";
  }
  if (["final_answer", "submit_failed", "submitting"].includes(state) && !draftDirty) {
    $("answer").value = view.final_answer_draft;
    $("answer-save-status").textContent = state === "submitting" ? "正在保存作答，请勿重复提交。" : "草稿已保存";
  }
  if (state) { renderMaterials(); renderMessages(); }
  const system = $("system-message");
  let notice = "";
  if (view?.operation_status === "pending") notice = "正在等待本轮回应……";
  if (view?.error) notice = view.error.message;
  if (state === "submit_failed") notice = notice || "未能保存作答，原文仍保留。";
  system.hidden = !notice;
  system.textContent = notice;
  system.classList.toggle("error", !!view?.error || state === "submit_failed");
  $("retry-turn").hidden = !currentTurn || !view?.error?.retryable || state !== "tutoring";
  $("turn-status").textContent = view?.error?.message || (view?.operation_status === "pending" ? "本轮处理中，输入暂不可编辑。" : "你的回应将接续此会话。");
  if (view?.operation_status === "pending" && state === "tutoring" && !pendingTimer) {
    const epoch = generation;
    const sessionId = view.session_id;
    pendingTimer = setTimeout(async () => {
      pendingTimer = null;
      const latest = await request("GET", `/api/sessions/${sessionId}`);
      if (epoch !== generation || view?.session_id !== sessionId) return;
      if (latest.body?.session_id) {
        remember(latest.body);
        render();
      } else {
        $("turn-status").textContent = errorText(latest, "暂时无法读取本轮状态，正在重试。" );
        render();
      }
    }, 1000);
  }
  if (view?.operation_status !== "pending" && pendingTimer) {
    clearTimeout(pendingTimer);
    pendingTimer = null;
  }
}
async function sendTurn(text, id = requestId()) {
  if (!view || busy) return;
  const epoch = generation;
  const turn = {request_id: id, text};
  currentTurn = turn;
  busy = true;
  $("reply").disabled = $("send").disabled = true;
  $("turn-status").textContent = "正在等待本轮回应……";
  const result = await request("POST", `/api/sessions/${view.session_id}/turns`, {
    request_id: id, expected_revision: view.revision, ...(text === null ? {} : {text})
  });
  if (epoch !== generation) return;
  busy = false;
  if (result.body?.session_id) { remember(result.body); render(); }
  else $("turn-status").textContent = errorText(result, "本轮未完成，请重试。");
  if (result.ok && result.body?.operation_status === "succeeded") {
    currentTurn = null;
    $("reply").value = "";
    render();
  }
}
function newProblem() {
  generation += 1;
  clearTimeout(pendingTimer);
  pendingTimer = null;
  localStorage.removeItem(STORAGE_KEY);
  view = null;
  currentTurn = null;
  recoveryProblem = false;
  draftDirty = false;
  busy = false;
  draftSerial = Promise.resolve(true);
  clearTimeout(draftTimer);
  $("start-form").reset();
  $("reply").value = "";
  $("answer").value = "";
  $("task-save-status").textContent = "尚未填写";
  render();
}
$("start-form").addEventListener("input", queueDraft);
$("retry-task-save").addEventListener("click", () => { void flushDraft(); });
$("retry-answer-save").addEventListener("click", () => { void flushDraft(); });
$("start-form").addEventListener("focusout", () => { if (draftDirty) void flushDraft(); });
$("start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  if (!hasTask(taskInput())) { $("task-save-status").textContent = "至少填写一项材料。"; return; }
  busy = true;
  const epoch = generation;
  const saved = await flushDraft();
  if (epoch !== generation) return;
  busy = false;
  if (!saved || draftDirty || !view) return;
  await sendTurn(null, view.session_id);
});
$("chat-composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("reply").value;
  if (!text.trim()) { $("turn-status").textContent = "请先输入本轮回应。"; return; }
  void sendTurn(text);
});
$("retry-turn").addEventListener("click", () => { if (currentTurn) void sendTurn(currentTurn.text, currentTurn.request_id); });
$("enter-answer").addEventListener("click", async () => {
  if (busy || !view) return;
  const epoch = generation;
  busy = true;
  const result = await request("POST", `/api/sessions/${view.session_id}/final-answer`, {request_id: requestId(), expected_revision: view.revision});
  if (epoch !== generation) return;
  busy = false;
  if (result.ok) { remember(result.body); render(); }
  else { $("system-message").hidden = false; $("system-message").textContent = errorText(result, "暂时无法进入最终作答。" ); }
});
$("answer").addEventListener("input", queueDraft);
$("answer").addEventListener("blur", () => { if (draftDirty) void flushDraft(); });
$("answer-composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !view) return;
  const epoch = generation;
  const answer = $("answer").value;
  if (!answer.trim()) { $("answer-save-status").textContent = "请先填写最终作答。"; return; }
  busy = true;
  const saved = await flushDraft();
  if (epoch !== generation) return;
  if (!saved || draftDirty) { busy = false; return; }
  $("answer").disabled = $("submit-answer").disabled = true;
  $("answer-save-status").textContent = "正在保存作答，请勿重复提交。";
  const result = await request("POST", `/api/sessions/${view.session_id}/submit`, {
    submission_id: view.submission_id, expected_revision: view.revision, final_answer: answer
  });
  if (epoch !== generation) return;
  busy = false;
  if (result.body?.session_id) { remember(result.body); render(); }
  else { $("answer").disabled = $("submit-answer").disabled = false; $("answer-save-status").textContent = errorText(result, "未能确认保存结果，请重试或刷新核对。"); }
});
$("new-problem").addEventListener("click", newProblem);
for (const button of document.querySelectorAll(".new-problem-action")) button.addEventListener("click", newProblem);
async function boot() {
  const epoch = generation;
  const id = localStorage.getItem(STORAGE_KEY);
  if (!id) { render(); return; }
  const result = await request("GET", `/api/sessions/${encodeURIComponent(id)}`);
  if (epoch !== generation) return;
  if (result.body?.session_id && (result.ok || result.status === 410 || result.status === 503)) {
    remember(result.body);
    if (view.error?.retryable && ["failed", "interrupted"].includes(view.operation_status) && view.state === "tutoring") {
      const last = view.conversation.at(-1);
      currentTurn = last?.role === "student"
        ? {request_id: last.event_id, text: last.text}
        : {request_id: view.session_id, text: null};
    }
    render();
    return;
  }
  recoveryProblem = true;
  render();
}
void boot();
