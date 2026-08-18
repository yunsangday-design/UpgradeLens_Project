/* UpgradeLens Agent Workbench — API client (ES Module) */

const BASE = "";

export async function postJSON(path, body) {
  const resp = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok && !data.error) data.error = `HTTP ${resp.status}`;
  return { status: resp.status, data };
}

export async function getJSON(path) {
  const resp = await fetch(BASE + path);
  return resp.json();
}

export function connectSSE(jobId, { onEvent, onDone, onError }) {
  const url = `${BASE}/api/jobs/${jobId}/events`;
  const es = new EventSource(url);
  const handler = (e) => {
    const payload = e.data ? JSON.parse(e.data) : {};
    if (onEvent) onEvent(e.type, payload, e);
  };
  es.addEventListener("job_started", handler);
  es.addEventListener("step_started", handler);
  es.addEventListener("step_finished", handler);
  es.addEventListener("plan.updated", handler);
  es.addEventListener("job_succeeded", (e) => {
    handler(e);
    es.close();
    if (onDone) getJSON(`/api/jobs/${jobId}`).then(snap => onDone(snap));
  });
  es.addEventListener("job_failed", (e) => {
    handler(e);
    es.close();
    if (onError) onError(JSON.parse(e.data || "{}"));
  });
  es.onerror = () => {
    es.close();
    // Attempt recovery
    setTimeout(() => {
      getJSON(`/api/jobs/${jobId}`).then(snap => {
        if (snap.status === "succeeded" && onDone) onDone(snap);
        else if (snap.status === "failed" && onError) onError(snap);
      }).catch(() => {});
    }, 1500);
  };
  return { close: () => es.close() };
}

export async function submitScan(repo) {
  return postJSON("/api/scan-async", { repo });
}

export async function submitAssessment({ goal, mode, dependency, target_version, source_version, repo }) {
  return postJSON("/api/run-async", { goal, mode, dependency, target_version, source_version, repo });
}

export async function submitCapability(payload) {
  return postJSON("/api/capability/run", payload);
}

export async function submitTask(payload) {
  return postJSON("/api/task/run", payload);
}

export async function getJobSnapshot(jobId) {
  return getJSON(`/api/jobs/${jobId}`);
}
