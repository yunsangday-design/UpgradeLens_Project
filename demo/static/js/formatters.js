/* UpgradeLens Agent Workbench — Formatters (ES Module) */

export function esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function el(tag, attrs, children) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const k in attrs) {
      if (k === "class") n.className = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
  }
  if (children != null) {
    (Array.isArray(children) ? children : [children]).forEach(c => {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
  }
  return n;
}

export function severityClass(severity) {
  return `severity-${(severity || "low").toLowerCase()}`;
}

export function statusBadge(status) {
  const map = {
    upgradable: "badge--green",
    up_to_date: "",
    unresolved: "badge--amber",
    lookup_failed: "badge--red",
  };
  return map[status] || "";
}

export function formatElapsed(ms) {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

export function verdictColor(verdict) {
  if (!verdict) return "";
  if (verdict.includes("needs") || verdict.includes("breaking")) return "var(--red)";
  if (verdict.includes("safe") || verdict.includes("compatible")) return "var(--green)";
  return "var(--amber)";
}
