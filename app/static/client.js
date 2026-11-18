// Shared by every page: sign-in (Identity Platform on GCP, developer sign-in locally), the API helper,
// the header, the legal-deadline clock, and the chat widget.
const SDK = "https://www.gstatic.com/firebasejs/13.1.0";
const config = await (await fetch("/api/config")).json();
let fb = null;
if (config.auth === "identity_platform") {
  const { initializeApp } = await import(`${SDK}/firebase-app.js`);
  const a = await import(`${SDK}/firebase-auth.js`);
  const auth = a.getAuth(initializeApp({ apiKey: config.apiKey, authDomain: `${config.projectId}.firebaseapp.com`, projectId: config.projectId }));
  await auth.authStateReady();
  fb = { auth, a };
}

export const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
export const day = iso => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
export const STATUS = { new: "New", in_review: "In review", resolved: "Resolved" };
export const status = s => `<span class="status ${esc(s)}">${STATUS[s] ?? esc(s)}</span>`;

// Days left until the legal deadline, with a bar showing how much of the allowed time is used.
export function clock(c) {
  if (c.status === "resolved") return `<div class="clock done"><b>Resolved</b><span>deadline was ${day(c.due)}</span></div>`;
  const start = new Date(c.created), due = new Date(c.due), now = Date.now();
  const days = Math.ceil(Math.abs(due - now) / 864e5), unit = days === 1 ? "day" : "days";
  const used = Math.min(100, Math.max(0, ((now - start) / (due - start)) * 100));
  const [cls, big, small] = due < now ? ["late", `${days} ${unit}`, `overdue since ${day(c.due)}`]
    : [days <= 2 ? "soon" : "", `${days} ${unit}`, `left, due ${day(c.due)}`];
  return `<div class="clock ${cls}" style="--used:${used.toFixed(0)}%"><b>${big}</b><span>${small}</span><i></i></div>`;
}

async function token() {
  if (fb) return fb.auth.currentUser ? fb.auth.currentUser.getIdToken() : null;
  const uid = localStorage.getItem("nw-dev-uid");
  return uid ? "dev:" + uid : null;
}

export async function signIn(email, password, create) {
  if (fb) return (create ? fb.a.createUserWithEmailAndPassword : fb.a.signInWithEmailAndPassword)(fb.auth, email, password);
  localStorage.setItem("nw-dev-uid", email.toLowerCase().replace(/[^a-z0-9]/g, "-").slice(0, 60));
}

// A throwaway account per visitor. Even in the staff view, visitors only see the sample cases and their own.
export const demoAccount = () => signIn(`demo-${crypto.randomUUID().slice(0, 12)}@example.com`, crypto.randomUUID(), true);

export async function signOut() {
  if (fb) await fb.a.signOut(fb.auth);
  localStorage.removeItem("nw-dev-uid");
  location.href = "/";
}

export async function api(path, options = {}) {
  const t = await token();
  const r = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(t && { Authorization: "Bearer " + t }) } });
  if (r.status === 401) { location.href = "/"; throw new Error("signed out"); }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// Documents go up as the raw file body and come back through the API, so access is checked on every read.
export async function upload(caseId, file) {
  const r = await fetch(`/api/my/cases/${encodeURIComponent(caseId)}/files?name=${encodeURIComponent(file.name)}`,
    { method: "POST", body: file, headers: { "Content-Type": file.type, Authorization: "Bearer " + (await token()) } });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "The document wasn't uploaded. Try again.");
  return r.json();
}

export async function download(caseId, index, name) {
  const r = await fetch(`/api/cases/${encodeURIComponent(caseId)}/files/${index}`, { headers: { Authorization: "Bearer " + (await token()) } });
  if (!r.ok) throw new Error("download failed");
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await r.blob()), download: name });
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export const docs = c => (c.files?.length ? `<div class="docs">${c.files.map((f, i) =>
  `<button type="button" class="link" data-doc="${i}" data-case="${esc(c.id)}" data-name="${esc(f.name)}">${esc(f.name)}</button>`).join("")}</div>` : "");

export function wireDownloads(root) {
  root.querySelectorAll("[data-doc]").forEach(b => (b.onclick = () => download(b.dataset.case, b.dataset.doc, b.dataset.name)));
}

export const me = async () => ((await token()) ? api("/api/me") : null);
export const home = user => (user.role === "staff" ? "/desk" : "/app");

export async function setRole(role) {
  const user = await api("/api/me/role", { method: "POST", body: JSON.stringify({ role }) });
  location.href = home(user);
}

export async function header(requiredRole) {
  const user = await me();
  if (!user) return void (location.href = "/");
  if (requiredRole && user.role !== requiredRole) return void (location.href = home(user));
  const staff = user.role === "staff";
  const links = staff ? [["/desk", "Case Desk"], ["/insights", "Insights"]] : [["/app", "Assistant"], ["/cases", "My cases"]];
  document.querySelector("header").innerHTML = `
    <a class="brand" href="/">Northwind Resolve</a>
    <nav aria-label="Main">${links.map(([h, t]) => `<a href="${h}" ${location.pathname === h ? 'aria-current="page"' : ""}>${t}</a>`).join("")}</nav>
    <div class="account">
      <button class="link" id="switch">Switch to ${staff ? "customer" : "staff"} view</button>
      <button id="out">Sign out</button>
    </div>`;
  document.getElementById("switch").onclick = () => setRole(staff ? "customer" : "staff");
  document.getElementById("out").onclick = signOut;
  runtime();
  return user;
}

// Which side of the runtime check this deployment is on: Google Cloud, or the local open-source stack.
export async function runtime() {
  const h = await (await fetch("/health")).json();
  const el = document.createElement("a");
  el.className = "runtime"; el.href = "/status";
  el.textContent = h.backend === "gcp" ? "Running on Google Cloud" : h.llm === "openai" ? "Running locally with OpenAI" : "Running on the local stack";
  el.title = `Model: ${h.model}\nEmbeddings: ${h.embeddings}\nSign-in: ${h.auth}\nStorage: ${h.store}\nEvents: ${h.events}`;
  document.querySelector("header").append(el);
}

export function chat(root, { endpoint, hello, suggestions, placeholder, label, onReply }) {
  root.classList.add("chat");
  root.innerHTML = `<div class="log" role="log" aria-live="polite"></div>
    <div class="suggestions" aria-label="Example questions"></div>
    <form><input maxlength="2000" autocomplete="off" aria-label="${esc(label)}" placeholder="${esc(placeholder)}"><button class="primary">Send</button></form>`;
  const log = root.querySelector(".log"), input = root.querySelector("input"), send = root.querySelector("form button");
  const session = crypto.randomUUID();
  const add = (cls, text) => { const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text; log.append(d); log.scrollTop = log.scrollHeight; return d; };
  add("bot", hello);
  for (const s of suggestions) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = s;
    b.onclick = () => { input.value = s; input.focus(); };
    root.querySelector(".suggestions").append(b);
  }
  root.querySelector("form").onsubmit = async e => {
    e.preventDefault();
    const text = input.value.trim(); if (!text || send.disabled) return;
    add("user", text); input.value = ""; send.disabled = true;
    root.querySelector(".suggestions").hidden = true; // examples are for starting a conversation
    const pending = add("bot pending", "Working on it…");
    try {
      const { reply } = await api(endpoint, { method: "POST", body: JSON.stringify({ session_id: session, message: text }) });
      pending.textContent = reply || "No answer came back. Send the message again.";
      onReply?.();
    } catch {
      pending.textContent = "The message didn't go through. Check your connection and send it again.";
      input.value = text;
    } finally {
      pending.classList.remove("pending"); send.disabled = false; input.focus();
    }
  };
}
