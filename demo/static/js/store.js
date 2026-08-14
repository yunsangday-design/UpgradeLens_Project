/* UpgradeLens Agent Workbench — State Store (ES Module) */

const _listeners = [];
let _state = {
  view: "home",         // home | scan | assess | running | result
  theme: localStorage.getItem("ul_theme") || "dark",
  project: { repo: "" },
  scan: { status: "idle", result: null, jobId: null },
  assess: { status: "idle", result: null, jobId: null, events: [] },
  drawer: { open: false, tab: "plan" },
};

export function getState() { return _state; }

export function setState(partial) {
  _state = { ..._state, ...partial };
  _listeners.forEach(fn => fn(_state));
}

export function updateNested(key, partial) {
  _state = { ..._state, [key]: { ..._state[key], ...partial } };
  _listeners.forEach(fn => fn(_state));
}

export function subscribe(fn) {
  _listeners.push(fn);
  return () => { const i = _listeners.indexOf(fn); if (i >= 0) _listeners.splice(i, 1); };
}

// Persist theme
subscribe((s) => localStorage.setItem("ul_theme", s.theme));

// Persist active job for refresh recovery
subscribe((s) => {
  if (s.assess.jobId && s.assess.status === "running") {
    sessionStorage.setItem("active_job_id", s.assess.jobId);
    sessionStorage.setItem("active_job_kind", "run");
  } else if (s.scan.jobId && s.scan.status === "running") {
    sessionStorage.setItem("active_job_id", s.scan.jobId);
    sessionStorage.setItem("active_job_kind", "scan");
  }
});

export function clearActiveJob() {
  sessionStorage.removeItem("active_job_id");
  sessionStorage.removeItem("active_job_kind");
}

export function getActiveJob() {
  return {
    jobId: sessionStorage.getItem("active_job_id"),
    kind: sessionStorage.getItem("active_job_kind"),
  };
}
