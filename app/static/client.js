// Shared by every page: sign-in (Identity Platform on GCP, developer sign-in locally), the API helper,
// the role-aware header, and the chat widget.
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

async function token() {
  if (fb) return fb.auth.currentUser ? fb.auth.currentUser.getIdToken() : null;
  const uid = localStorage.getItem("nw-dev-uid");
  return uid ? "dev:" + uid : null;
}

export async function signIn(email, password, create) {
  if (fb) return (create ? fb.a.createUserWithEmailAndPassword : fb.a.signInWithEmailAndPassword)(fb.auth, email, password);
  localStorage.setItem("nw-dev-uid", email.toLowerCase().replace(/[^a-z0-9]/g, "-").slice(0, 60));
}

// A throwaway account per visitor, so demo users never see each other's cases.
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
  const links = user.role === "staff" ? [["/desk", "Case Desk"], ["/insights", "Insights"]] : [["/app", "My account"]];
  const other = user.role === "staff" ? "customer" : "staff";
  document.querySelector("header").innerHTML = `
    <a class="brand" href="/">Northwind Resolve</a>
    <nav>${links.map(([h, t]) => `<a href="${h}" class="${location.pathname === h ? "on" : ""}">${t}</a>`).join("")}</nav>
    <span class="who">Signed in as ${user.role} · <a href="#" id="switch">view as ${other} (demo)</a> · <a href="#" id="out">sign out</a></span>`;
  document.getElementById("switch").onclick = e => { e.preventDefault(); setRole(other); };
  document.getElementById("out").onclick = e => { e.preventDefault(); signOut(); };
  badge();
  return user;
}

export function chat(root, { endpoint, hello, chips, placeholder, onReply }) {
  root.classList.add("chat");
  root.innerHTML = `<div class="log"></div><div class="chips"></div>
    <form><input maxlength="2000" autocomplete="off" placeholder="${esc(placeholder)}"><button class="primary">Send</button></form>`;
  const log = root.querySelector(".log"), input = root.querySelector("input"), session = crypto.randomUUID();
  const add = (cls, text) => { const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text; log.append(d); log.scrollTop = log.scrollHeight; return d; };
  add("bot", hello);
  for (const c of chips) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = c;
    b.onclick = () => { input.value = c; input.focus(); };
    root.querySelector(".chips").append(b);
  }
  root.querySelector("form").onsubmit = async e => {
    e.preventDefault();
    const text = input.value.trim(); if (!text) return;
    add("user", text); input.value = "";
    const pending = add("bot", "…");
    try {
      pending.textContent = (await api(endpoint, { method: "POST", body: JSON.stringify({ session_id: session, message: text }) })).reply || "(no reply)";
      onReply?.();
    } catch { pending.textContent = "Sorry, that didn't work. Try again."; }
  };
}

// Which side of the runtime check this deployment is on (GCP or the local open-source stack).
export async function badge() {
  const h = await (await fetch("/health")).json();
  const b = document.createElement("span");
  b.className = "badge " + h.backend;
  b.textContent = (h.backend === "gcp" ? "● GCP · " : "● Local · ") + h.model;
  b.title = `auth: ${h.auth}\nembeddings: ${h.embeddings}\nstore: ${h.store}\nevents: ${h.events}`;
  document.querySelector("header").append(b);
}
