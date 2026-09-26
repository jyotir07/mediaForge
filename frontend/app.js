"use strict";

const $ = (id) => document.getElementById(id);
const TERMINAL = new Set(["SUCCEEDED", "FAILED"]);
let mediaId = null;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(body?.detail ? JSON.stringify(body.detail) : `${res.status} ${res.statusText}`);
  return body;
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text); // never innerHTML: model output is untrusted
  if (className) node.className = className;
  return node;
}

function fmt(seconds) {
  return `${Number(seconds).toFixed(1)}s`;
}

// One table row per job, updated from the job's SSE stream until it reaches a terminal state.
function watchJob(jobId, label) {
  $("pipeline").hidden = false;
  const row = el("tr");
  const status = el("span", "QUEUED", "status QUEUED");
  const bar = el("div");
  const barWrap = el("div", null, "bar");
  barWrap.append(bar);
  const message = el("span", "", "muted");
  const statusCell = el("td");
  statusCell.append(status);
  const barCell = el("td");
  barCell.append(barWrap);
  row.append(el("td", label), statusCell, barCell, el("td"));
  row.lastChild.append(message);
  $("jobs").append(row);

  return new Promise((resolve) => {
    const source = new EventSource(`/jobs/${jobId}/events`);
    source.addEventListener("progress", async (event) => {
      const data = JSON.parse(event.data);
      status.textContent = data.status;
      status.className = `status ${data.status}`;
      if (typeof data.progress === "number") bar.style.width = `${Math.round(data.progress * 100)}%`;
      if (data.message) message.textContent = data.stage ? `${data.stage}: ${data.message}` : data.message;
      if (data.error_message) message.textContent = `${data.error_code}: ${data.error_message}`;
      if (TERMINAL.has(data.status)) {
        source.close();
        const job = await api(`/jobs/${jobId}`);
        if (job.status === "FAILED") message.textContent = `${job.error_code}: ${job.error_message}`;
        resolve(job);
      }
    });
    source.onerror = () => {}; // EventSource reconnects on its own; the server re-sends a snapshot.
  });
}

async function refreshMedia() {
  const media = await api(`/media/${mediaId}`);
  const byKey = Object.fromEntries(media.artifacts.map((a) => [a.storage_key, a]));
  const sheet = media.artifacts.find((a) => a.type === "THUMBNAIL");
  if (sheet) {
    $("sheet").src = `/artifacts/${sheet.id}/file`;
    $("sheet").hidden = false;
  }
  return { media, byKey };
}

async function showMetadata() {
  const meta = await api(`/media/${mediaId}/metadata`);
  const items = {
    Duration: fmt(meta.duration_seconds),
    Resolution: `${meta.width}x${meta.height}`,
    FPS: meta.fps.toFixed(2),
    Video: meta.video_codec,
    Audio: meta.audio_codec ?? "none",
    Container: meta.container,
    Size: `${(meta.size_bytes / 1e6).toFixed(1)} MB`,
  };
  $("meta").replaceChildren(
    ...Object.entries(items).map(([k, v]) => {
      const d = el("div", v);
      d.prepend(el("span", k));
      return d;
    })
  );
}

async function showSegments() {
  const { media, byKey } = await refreshMedia();
  const analysis = media.artifacts.find((a) => a.type === "ANALYSIS");
  if (!analysis) return;
  const doc = await api(`/artifacts/${analysis.id}/file`);
  $("summary").textContent = doc.overall_summary;
  $("segment-rows").replaceChildren(
    ...doc.segments.map((s) => {
      const tr = el("tr");
      const frameCell = el("td");
      const frame = s.frame_key && byKey[s.frame_key];
      if (frame) {
        const img = el("img", null, "frame");
        img.src = `/artifacts/${frame.id}/file`;
        img.alt = `Segment ${s.index}`;
        frameCell.append(img);
      }
      tr.append(
        frameCell,
        el("td", `${fmt(s.start)}–${fmt(s.end)}`),
        el("td", s.summary),
        el("td", s.signals.llm_relevance.toFixed(2)),
        el("td", s.signals.audio_presence.toFixed(2))
      );
      return tr;
    })
  );
  $("segments").hidden = false;
  $("edit").hidden = false;
}

async function showDecisions(planId) {
  const plan = await api(`/edit-plans/${planId}`);
  const accepted = new Set(plan.accepted_operations.map((o) => `${o.start}-${o.end}`));
  $("decision-rows").replaceChildren(
    ...plan.decisions.map((d) => {
      const tr = el("tr");
      const conf = (c) => (c === null || c === undefined ? "" : ` (${Number(c).toFixed(2)})`);
      tr.append(
        el("td", `${fmt(d.start)}–${fmt(d.end)}`),
        el("td", d.reason),
        el("td", `${d.verdict} · ${d.verdict_source}${conf(d.verdict_confidence)}`),
        el("td", d.value ? `${d.value} · ${d.value_source}${conf(d.value_confidence)}` : "—")
      );
      return tr;
    })
  );
  $("decisions").hidden = false;
  return accepted;
}

async function showExport(exportId) {
  const exp = await api(`/exports/${exportId}`);
  if (exp.status !== "READY") return;
  const items = {
    Duration: fmt(exp.duration_seconds),
    Resolution: `${exp.width}x${exp.height}`,
    Codec: exp.video_codec,
  };
  $("export-meta").replaceChildren(
    ...Object.entries(items).map(([k, v]) => {
      const d = el("div", v);
      d.prepend(el("span", k));
      return d;
    })
  );
  $("video").src = exp.download_url;
  $("result").hidden = false;
}

$("file").addEventListener("change", () => ($("upload-btn").disabled = !$("file").files.length));

$("upload-btn").addEventListener("click", () => {
  const file = $("file").files[0];
  $("upload-err").textContent = "";
  $("upload-btn").disabled = true;
  // The API takes the raw body (not multipart) so the server can enforce the size limit while streaming.
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/media?filename=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("upload-bar").style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
  };
  xhr.onload = async () => {
    if (xhr.status !== 201) {
      $("upload-err").textContent = `Upload failed: ${xhr.status} ${xhr.responseText}`;
      $("upload-btn").disabled = false;
      return;
    }
    const body = JSON.parse(xhr.responseText);
    mediaId = body.media_id;
    $("media").hidden = false;
    const probe = await watchJob(body.probe_job_id, "Probe");
    if (probe.status === "SUCCEEDED") {
      await showMetadata();
      $("proxy-btn").disabled = false;
      $("analyze-btn").disabled = false;
    }
  };
  xhr.onerror = () => {
    $("upload-err").textContent = "Upload failed: network error";
    $("upload-btn").disabled = false;
  };
  xhr.send(file);
});

$("proxy-btn").addEventListener("click", async () => {
  $("proxy-btn").disabled = true;
  const { job_id } = await api(`/media/${mediaId}/proxy`, { method: "POST" });
  await watchJob(job_id, "Proxy + thumbnails");
  await refreshMedia();
});

$("analyze-btn").addEventListener("click", async () => {
  $("analyze-btn").disabled = true;
  const { job_id } = await api(`/media/${mediaId}/analyze`, { method: "POST" });
  const job = await watchJob(job_id, "Analysis");
  if (job.status === "SUCCEEDED") await showSegments();
  else $("analyze-btn").disabled = false;
});

$("edit-btn").addEventListener("click", async () => {
  $("edit-btn").disabled = true;
  try {
    const { job_id } = await api(`/media/${mediaId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: $("request").value }),
    });
    const planning = await watchJob(job_id, "Edit planning + Jev decisions");
    if (planning.status !== "SUCCEEDED") return;
    await showDecisions(planning.result.edit_plan_id);
    const exportJob = await watchJob(planning.result.export_job_id, "Export");
    if (exportJob.status === "SUCCEEDED") await showExport(planning.result.export_id);
  } catch (err) {
    alert(err.message);
  } finally {
    $("edit-btn").disabled = false;
  }
});
