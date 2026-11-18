// Shared header: nav links plus a badge showing which side of the runtime check is active.
document.querySelector("header").innerHTML = `
  <a class="brand" href="/">Northwind Bank · Customer Operations</a>
  <nav>${[["/assistant", "Customer assistant"], ["/desk", "Case Desk"], ["/analyst", "Analyst"],
          ["https://github.com/daMS2005/complaint-intake-agent", "Code"]]
    .map(([h, t]) => `<a href="${h}" class="${location.pathname === h ? "on" : ""}">${t}</a>`).join("")}</nav>
  <span class="badge" id="badge">checking backend…</span>`;
fetch("/health").then(r => r.json()).then(h => {
  const b = document.getElementById("badge");
  b.className = "badge " + h.backend;
  b.textContent = (h.backend === "gcp" ? "● GCP · " : "● Local · ") + h.model;
  b.title = `embeddings: ${h.embeddings}\ncases: ${h.cases}\nevents: ${h.events}`;
});
