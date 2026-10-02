"use strict";

// The homepage is a fixed preview. Only the Socratic handoff calls the API.
const $ = (id) => document.getElementById(id);
const sessionKey = "als.active_session_id";
const startKey = "als.v21.start_request_id";
const turnKey = "als.v21.turn";
let view = null;
let busy = false;
let submitting = false;
let retryAction = null;
let pollTimer = null;
let queuedReply = null;
let renderedConversation = null;

async function api(method, path, body) {
  try {
    const response = await fetch(path, {
      method, cache: "no-store", signal: AbortSignal.timeout(path.endsWith('/turns') ? 100000 : 10000),
      headers: body === undefined ? undefined : {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    return {ok: response.ok, data: await response.json()};
  } catch (_) { return {ok: false, data: null}; }
}
function updateSystemNotice() { $("system-notice").hidden = !$("tutor-status").textContent && !retryAction; }
function status(message, kind = "info") {
  $("tutor-status").textContent = message;
  $("tutor-status").title = message;
  $("system-notice").dataset.kind = kind;
  updateSystemNotice();
}
function showTutor() { $("home").hidden = true; $("tutor").hidden = false; window.scrollTo(0, 0); }
function setRetry(action) { retryAction = action; $("retry-tutor").hidden = !action; updateSystemNotice(); }
function hasTeacherQuestion() { return (view?.conversation || []).some((message) => message.role === "assistant"); }
function updateSend() {
  const canRetryOpening = view?.error?.retryable && !hasTeacherQuestion();
  const waitingForOpening = !hasTeacherQuestion();
  const openingRequest = JSON.parse(sessionStorage.getItem(turnKey) || "null");
  const canReply = view?.state === "tutoring" || (view?.state === "help_input" && openingRequest?.text === null);
  $("tutor-send").disabled = !canReply || (busy && !waitingForOpening) || submitting || !!queuedReply ||
    (view.operation_status === "pending" && !waitingForOpening) || !$("tutor-reply").value.trim() ||
    (!!view.error && !canRetryOpening);
}
function renderBrief(input) {
  const problem = input?.problem?.trim() || "";
  const attempts = input?.attempts?.trim() || "";
  const answer = input?.original_answer?.trim() || "";
  const work = answer && !attempts.includes(answer) ? [attempts, answer].filter(Boolean).join("\n") : attempts || answer;
  const concern = input?.concern?.trim() || "";
  const uncertainty = concern.match(/I[’']m not sure[^.]*\./i)?.[0] || concern;
  for (const [name, value] of [["task", problem], ["attempt", work], ["concern", uncertainty]]) {
    $("brief-" + name).textContent = value;
    $("brief-" + name + "-card").hidden = !value;
  }
}
function appendMessage(list, message, queued = false) {
  const row = document.createElement("article");
  row.className = `tutor-message ${message.role}${queued ? " queued" : ""}`;
  const identity = document.createElement("div");
  identity.className = "tutor-identity";
  const avatar = document.createElement("img");
  avatar.src = message.role === "student" ? "/assets/john-avatar.png" : "/assets/socratic-avatar.jpg";
  avatar.alt = "";
  const name = document.createElement("small");
  name.textContent = message.role === "student" ? "John" : "Socratic";
  identity.append(avatar, name);
  const bubble = document.createElement("div");
  bubble.className = "tutor-bubble";
  const body = document.createElement("p");
  body.textContent = message.text;
  bubble.append(body);
  if (queued) {
    const delivery = document.createElement("small");
    delivery.className = "delivery-note";
    delivery.textContent = queuedReply.state === "waiting" ? "Waiting for teacher" :
      queuedReply.state === "unconfirmed" ? "Delivery unconfirmed" : "Sending…";
    bubble.append(delivery);
  }
  row.append(identity, bubble);
  list.append(row);
}
function renderConversation(next) {
  const messages = next.conversation || [];
  const savedStudentCount = messages.filter((message) => message.role === "student").length;
  if (queuedReply && savedStudentCount > queuedReply.previousStudentCount) queuedReply = null;
  const content = JSON.stringify([messages, queuedReply]);
  if (content === renderedConversation) return;
  renderedConversation = content;
  const list = $("tutor-messages");
  list.replaceChildren();
  for (const message of messages) appendMessage(list, message);
  if (queuedReply) appendMessage(list, {role: "student", text: queuedReply.text}, true);
  requestAnimationFrame(() => {
    const pane = list.parentElement;
    pane.scrollTop = pane.scrollHeight;
  });
}
function showView(next) {
  if (!next?.session_id) return;
  view = next;
  localStorage.setItem(sessionKey, next.session_id);
  showTutor();
  renderBrief(next.task_input);
  renderConversation(next);
  $("practice-next").disabled = next.state !== "ready_for_submission" || !!next.error;
  $("tutor-form").hidden = next.state !== "tutoring";
  $("tutor-reply").disabled = next.state !== "tutoring";
  updateSend();
  if (next.state === "ready_for_submission") { status(""); setRetry(null); }
  else if (next.operation_status === "pending") { status("Waiting for your teacher's response…"); schedulePoll(); }
  else if (next.error) {
    const errors = {
      provider_authentication: "The teacher connection rejected the API Key. Update the local Key, then retry. Your text is preserved.",
      provider_balance: "The teacher connection has insufficient credit. Add credit, then retry. Your text is preserved.",
      provider_configuration: "The teacher connection settings were rejected. Check the configured model and address, then retry.",
      provider_connection: "Unable to connect to your teacher. Your text is preserved; please retry.",
      provider_rate_limit: "Your teacher is temporarily busy. Your text is preserved; please retry shortly."
    };
    errors.runtime_timeout = "Socratic did not respond in time. Your text is preserved; please retry.";
    status(errors[next.error.code] || (next.error.retryable ? "This response did not finish. Your text is preserved; please retry." : "This session cannot continue."), "error");
    setRetry(next.error.retryable ? retryLastTurn : null);
  } else { status(""); setRetry(null); }
}
function schedulePoll() {
  if (pollTimer) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    if (!view?.session_id) return;
    const result = await api("GET", `/api/sessions/${view.session_id}`);
    if (result.data?.session_id) showView(result.data);
    else { status("Unable to read the response. Please retry."); setRetry(refresh); }
  }, 1000);
}
async function refresh() {
  if (!view?.session_id) return;
  const result = await api("GET", `/api/sessions/${view.session_id}`);
  if (result.data?.session_id) showView(result.data);
  else { status("Unable to reopen this session. Your work has not been cleared."); setRetry(refresh); }
}
async function sendTurn(text, id) {
  if (!view || busy) return;
  busy = true;
  if (text !== null && queuedReply) { queuedReply.state = "sending"; renderConversation(view); }
  status("Connecting to your teacher…"); setRetry(null);
  if (text === null) { $("tutor-form").hidden = false; $("tutor-reply").disabled = false; }
  $("tutor-send").disabled = true;
  sessionStorage.setItem(turnKey, JSON.stringify({id, text}));
  updateSend();
  const result = await api("POST", `/api/sessions/${view.session_id}/turns`, {request_id: id, expected_revision: view.revision, text});
  busy = false;
  if (result.data?.session_id) {
    showView(result.data);
    if (result.ok && result.data.operation_status !== "pending") {
      sessionStorage.removeItem(turnKey);
      updateSend();
    }
  } else {
    if (text !== null && queuedReply) { queuedReply.state = "unconfirmed"; renderConversation(view); }
    status("Connection lost. Your message is still here; please retry.", "error");
    setRetry(retryLastTurn);
  }
  updateSend();
  if (text === null && hasTeacherQuestion() && !view.error && view.operation_status !== "pending" && queuedReply) {
    await sendTurn(queuedReply.text, crypto.randomUUID());
  }
}
async function retryLastTurn() {
  const saved = JSON.parse(sessionStorage.getItem(turnKey) || "null");
  await refresh();
  if (!saved && view?.error?.retryable && view.state === "tutoring" && !view.conversation?.length) {
    await sendTurn(null, view.session_id);
    return;
  }
  if (!saved) return;
  if (view?.error?.retryable || view?.operation_status !== "succeeded" ||
      (saved.text === null && !hasTeacherQuestion()) || (saved.text !== null && queuedReply)) {
    await sendTurn(saved.text, saved.id);
  }
}
async function prepare(current) {
  if (current.state !== "help_input" || busy) return;
  busy = true; status("Preparing your question…");
  if (!current.task_input?.problem) {
    const saved = await api("PUT", `/api/sessions/${current.session_id}/draft`, {
      request_id: crypto.randomUUID(), expected_revision: current.revision, use_learning_brief: true
    });
    if (!saved.ok || !saved.data?.session_id) {
      busy = false; status("Could not prepare the question. Please retry.");
      setRetry(async () => { await refresh(); await prepare(view); }); return;
    }
    showView(saved.data);
  }
  busy = false;
  await sendTurn(null, current.session_id);
}
async function begin() {
  if (busy) return;
  if (localStorage.getItem(sessionKey)) { await reopen(); return; }
  busy = true; showTutor(); status("Starting your conversation…"); setRetry(null);
  const createId = sessionStorage.getItem(startKey) || crypto.randomUUID();
  sessionStorage.setItem(startKey, createId);
  const created = await api("POST", "/api/sessions", {request_id: createId});
  busy = false;
  if (!created.ok || !created.data?.session_id) { status("Could not start the conversation. Please retry."); setRetry(begin); return; }
  sessionStorage.removeItem(startKey);
  showView(created.data);
  await prepare(created.data);
}
async function reopen() {
  const id = localStorage.getItem(sessionKey);
  if (!id) return;
  showTutor(); status("Reopening your conversation…");
  const result = await api("GET", `/api/sessions/${id}`);
  if (result.data?.session_id) {
    showView(result.data);
    if (result.data.state === "help_input") await prepare(result.data);
    else if (result.data.error?.retryable && !hasTeacherQuestion()) await retryLastTurn();
  } else { status("Unable to reopen this session. Your work has not been cleared."); setRetry(reopen); }
}
$("ask-teacher").addEventListener("click", begin);
$("retry-tutor").addEventListener("click", () => { if (retryAction) void retryAction(); });
$("practice-next").addEventListener("click", () => { $("practice-notice").hidden = false; });
$("tutor-reply").addEventListener("input", updateSend);
$("tutor-reply").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$("tutor-send").disabled) $("tutor-form").requestSubmit();
  }
});
$("tutor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("tutor-reply").value.trim();
  if (!text || submitting || $("tutor-send").disabled) return;
  if (busy && !hasTeacherQuestion()) {
    queuedReply = {text, previousStudentCount: 0, state: "waiting"};
    $("tutor-reply").value = "";
    renderConversation(view);
    updateSend();
    return;
  }
  submitting = true;
  queuedReply = {text, previousStudentCount: (view.conversation || []).filter((message) => message.role === "student").length, state: hasTeacherQuestion() ? "sending" : "waiting"};
  $("tutor-reply").value = "";
  renderConversation(view);
  updateSend();
  try {
    if (!hasTeacherQuestion()) {
      await retryLastTurn();
      if (!hasTeacherQuestion() || view.error) {
        if (queuedReply) { queuedReply.state = "waiting"; renderConversation(view); }
        return;
      }
    }
    if (queuedReply) await sendTurn(text, crypto.randomUUID());
  } finally {
    submitting = false;
    updateSend();
  }
});
