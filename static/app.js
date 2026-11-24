(() => {
  "use strict";

  const YT_REGEX =
    /(?:https?:\/\/)?(?:www\.|m\.|music\.)?(?:youtube\.com\/(?:watch\?(?:[^\s]*&)?v=|shorts\/|live\/|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/i;
  const HISTORY_KEY = "srking_recent_yt_v1";
  const MAX_HISTORY = 8;

  const els = {
    serverStatus: document.getElementById("serverStatus"),
    inspectForm: document.getElementById("inspectForm"),
    urlInput: document.getElementById("urlInput"),
    clearBtn: document.getElementById("clearBtn"),
    pasteBtn: document.getElementById("pasteBtn"),
    fetchBtn: document.getElementById("fetchBtn"),
    fetchSpinner: document.getElementById("fetchSpinner"),
    inputError: document.getElementById("inputError"),
    inputErrorText: document.getElementById("inputErrorText"),

    mediaPanel: document.getElementById("mediaPanel"),
    mediaThumb: document.getElementById("mediaThumb"),
    mediaDuration: document.getElementById("mediaDuration"),
    mediaTitle: document.getElementById("mediaTitle"),
    mediaChannel: document.getElementById("mediaChannel"),
    mediaSourceLink: document.getElementById("mediaSourceLink"),
    tabButtons: Array.from(document.querySelectorAll(".tab-btn")),
    spatialNotice: document.getElementById("spatialNotice"),
    qualityList: document.getElementById("qualityList"),
    selectedQualityText: document.getElementById("selectedQualityText"),
    downloadBtn: document.getElementById("downloadBtn"),

    jobPanel: document.getElementById("jobPanel"),
    jobStateBadge: document.getElementById("jobStateBadge"),
    jobStage: document.getElementById("jobStage"),
    jobPercent: document.getElementById("jobPercent"),
    progressTrack: document.getElementById("progressTrack"),
    progressFill: document.getElementById("progressFill"),
    jobSpeed: document.getElementById("jobSpeed"),
    jobEta: document.getElementById("jobEta"),
    jobBpm: document.getElementById("jobBpm"),
    jobErrorBox: document.getElementById("jobErrorBox"),
    jobErrorText: document.getElementById("jobErrorText"),
    cancelJobBtn: document.getElementById("cancelJobBtn"),
    saveFileBtn: document.getElementById("saveFileBtn"),
    retryJobBtn: document.getElementById("retryJobBtn"),
    newDownloadBtn: document.getElementById("newDownloadBtn"),

    historySection: document.getElementById("historySection"),
    historyList: document.getElementById("historyList"),
    clearHistoryBtn: document.getElementById("clearHistoryBtn"),
  };

  let state = {
    media: null,
    activeTab: "video",
    selectedQuality: null,
    currentJobId: null,
    eventSource: null,
    pollTimer: null,
    autoTriggeredJobId: null,
  };

  // ---------------------------------------------------------------------------
  // Server Health Check
  // ---------------------------------------------------------------------------
  async function checkHealth() {
    const label = els.serverStatus.querySelector(".status-label");
    try {
      const res = await fetch("/health", { cache: "no-store" });
      if (!res.ok) throw new Error("Unhealthy");
      const data = await res.json();
      els.serverStatus.classList.remove("offline");
      els.serverStatus.classList.add("online");
      label.textContent = data.ffmpeg ? "Online · FFmpeg Ready" : "Online";
    } catch {
      els.serverStatus.classList.remove("online");
      els.serverStatus.classList.add("offline");
      label.textContent = "Server Offline";
    }
  }

  // ---------------------------------------------------------------------------
  // Error Helpers
  // ---------------------------------------------------------------------------
  function showInputError(msg) {
    els.inputErrorText.textContent = msg;
    els.inputError.classList.remove("hidden");
  }

  function clearInputError() {
    els.inputError.classList.add("hidden");
    els.inputErrorText.textContent = "";
  }

  function setFetching(loading) {
    els.fetchBtn.disabled = loading;
    els.fetchBtn.querySelector(".btn-label").textContent = loading ? "Inspecting" : "Fetch Formats";
    els.fetchSpinner.classList.toggle("hidden", !loading);
  }

  // ---------------------------------------------------------------------------
  // Local Recent History
  // ---------------------------------------------------------------------------
  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function saveHistoryItem(item) {
    const list = loadHistory().filter((x) => x.video_id !== item.video_id);
    list.unshift({
      video_id: item.video_id,
      url: item.url,
      title: item.title,
      duration_text: item.duration_text,
    });
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)));
    } catch {
      // Ignore quota errors
    }
    renderHistory();
  }

  function renderHistory() {
    const list = loadHistory();
    if (!list.length) {
      els.historySection.classList.add("hidden");
      return;
    }
    els.historySection.classList.remove("hidden");
    els.historyList.innerHTML = "";
    list.forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "history-item";

      const titleSpan = document.createElement("span");
      titleSpan.className = "history-title";
      titleSpan.textContent = item.title || item.url;

      const metaSpan = document.createElement("span");
      metaSpan.className = "history-meta mono";
      metaSpan.textContent = item.duration_text || "";

      btn.appendChild(titleSpan);
      btn.appendChild(metaSpan);
      btn.addEventListener("click", () => {
        els.urlInput.value = item.url;
        updateClearButton();
        inspectUrl(item.url);
      });
      els.historyList.appendChild(btn);
    });
  }

  // ---------------------------------------------------------------------------
  // Inspect YouTube URL
  // ---------------------------------------------------------------------------
  async function inspectUrl(rawUrl) {
    clearInputError();
    const trimmed = (rawUrl || "").trim();
    if (!YT_REGEX.test(trimmed)) {
      showInputError("Only valid YouTube links are supported (youtube.com/watch, youtu.be, shorts, music.youtube.com).");
      return;
    }

    stopJobTracking();
    els.jobPanel.classList.add("hidden");
    setFetching(true);

    try {
      const res = await fetch("/api/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: trimmed }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || "Could not inspect this YouTube link.");
      }
      state.media = data;
      saveHistoryItem(data);
      renderMediaPanel(data);
    } catch (err) {
      els.mediaPanel.classList.add("hidden");
      showInputError(err.message || "Network error while inspecting link.");
    } finally {
      setFetching(false);
    }
  }

  function renderMediaPanel(media) {
    els.mediaThumb.src = media.thumbnail;
    els.mediaThumb.alt = media.title;
    els.mediaDuration.textContent = media.duration_text || "--:--";
    els.mediaTitle.textContent = media.title;
    els.mediaChannel.textContent = media.channel;
    els.mediaSourceLink.href = media.url;

    els.mediaPanel.classList.remove("hidden");
    selectTab(state.activeTab || "video");
  }

  function getOptionsForTab(tab) {
    if (!state.media) return [];
    if (tab === "video") return state.media.video_options || [];
    if (tab === "audio") return state.media.audio_options || [];
    if (tab === "spatial16d") return state.media.spatial_options || [];
    return [];
  }

  function selectTab(tab) {
    state.activeTab = tab;
    els.tabButtons.forEach((btn) => {
      const isActive = btn.dataset.tab === tab;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", String(isActive));
    });

    els.spatialNotice.classList.toggle("hidden", tab !== "spatial16d");
    const options = getOptionsForTab(tab);
    state.selectedQuality = options.length ? options[0].id : null;
    renderQualityOptions(options);
  }

  function renderQualityOptions(options) {
    els.qualityList.innerHTML = "";
    options.forEach((opt) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "quality-option" + (opt.id === state.selectedQuality ? " selected" : "");
      btn.setAttribute("role", "radio");
      btn.setAttribute("aria-checked", String(opt.id === state.selectedQuality));

      const left = document.createElement("div");
      left.className = "quality-left";

      const dot = document.createElement("span");
      dot.className = "radio-dot";
      dot.setAttribute("aria-hidden", "true");

      const textWrap = document.createElement("div");
      textWrap.className = "quality-text";

      const titleRow = document.createElement("div");
      titleRow.className = "quality-title-row";

      const labelEl = document.createElement("span");
      labelEl.className = "quality-label";
      labelEl.textContent = opt.label;
      titleRow.appendChild(labelEl);

      if (opt.fps && opt.fps >= 50) {
        const fpsPill = document.createElement("span");
        fpsPill.className = "tag-pill";
        fpsPill.textContent = `${opt.fps}fps`;
        titleRow.appendChild(fpsPill);
      }

      if (opt.container || opt.ext) {
        const extPill = document.createElement("span");
        extPill.className = "tag-pill";
        extPill.textContent = opt.container || opt.ext;
        titleRow.appendChild(extPill);
      }

      if (opt.badge) {
        const badgePill = document.createElement("span");
        badgePill.className = "tag-pill";
        badgePill.textContent = opt.badge;
        titleRow.appendChild(badgePill);
      }

      textWrap.appendChild(titleRow);

      if (opt.desc) {
        const descEl = document.createElement("span");
        descEl.className = "quality-desc";
        descEl.textContent = opt.desc;
        textWrap.appendChild(descEl);
      }

      left.appendChild(dot);
      left.appendChild(textWrap);

      const sizeEl = document.createElement("span");
      sizeEl.className = "quality-size mono";
      sizeEl.textContent = opt.estimated_size ? `~${opt.estimated_size}` : "Auto";

      btn.appendChild(left);
      btn.appendChild(sizeEl);

      btn.addEventListener("click", () => {
        state.selectedQuality = opt.id;
        renderQualityOptions(options);
      });

      els.qualityList.appendChild(btn);
    });

    const chosen = options.find((o) => o.id === state.selectedQuality);
    if (chosen) {
      const sz = chosen.estimated_size ? ` · ~${chosen.estimated_size}` : "";
      els.selectedQualityText.textContent = `${chosen.label}${sz}`;
      els.downloadBtn.disabled = false;
    } else {
      els.selectedQualityText.textContent = "—";
      els.downloadBtn.disabled = true;
    }
  }

  // ---------------------------------------------------------------------------
  // Start Download Job & Track Progress (SSE + Fallback Polling)
  // ---------------------------------------------------------------------------
  function stopJobTracking() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function startDownload() {
    if (!state.media || !state.selectedQuality) return;

    stopJobTracking();
    clearInputError();
    els.downloadBtn.disabled = true;
    state.autoTriggeredJobId = null;

    els.jobPanel.classList.remove("hidden");
    els.jobErrorBox.classList.add("hidden");
    els.saveFileBtn.classList.add("hidden");
    els.retryJobBtn.classList.add("hidden");
    els.newDownloadBtn.classList.add("hidden");
    els.cancelJobBtn.classList.remove("hidden");
    els.cancelJobBtn.disabled = false;
    els.jobBpm.classList.add("hidden");
    els.progressFill.classList.remove("ready");
    els.progressFill.style.width = "3%";
    els.jobPercent.textContent = "3%";
    els.jobStage.textContent = "Creating download session";
    els.jobStateBadge.className = "badge badge-running";
    els.jobStateBadge.textContent = "Processing";

    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: state.media.url,
          media_type: state.activeTab,
          quality: state.selectedQuality,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || "Failed to start download.");
      }

      state.currentJobId = data.job_id;
      updateJobUI(data);
      subscribeToJob(data.job_id);
    } catch (err) {
      els.downloadBtn.disabled = false;
      els.cancelJobBtn.classList.add("hidden");
      els.retryJobBtn.classList.remove("hidden");
      els.jobStateBadge.className = "badge badge-error";
      els.jobStateBadge.textContent = "Error";
      els.jobStage.textContent = "Could not start download";
      els.jobErrorText.textContent = err.message || "Unexpected error.";
      els.jobErrorBox.classList.remove("hidden");
    }
  }

  function subscribeToJob(jobId) {
    stopJobTracking();

    if (typeof EventSource !== "undefined") {
      const es = new EventSource(`/api/jobs/${jobId}/events`);
      state.eventSource = es;

      es.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          updateJobUI(data);
          if (["ready", "error", "cancelled"].includes(data.status)) {
            stopJobTracking();
          }
        } catch {
          // Ignore malformed keep-alive
        }
      };

      es.onerror = () => {
        // Fallback to polling if proxy interrupts SSE
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
        startPollingFallback(jobId);
      };
    } else {
      startPollingFallback(jobId);
    }
  }

  function startPollingFallback(jobId) {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
        if (!res.ok) {
          stopJobTracking();
          return;
        }
        const data = await res.json();
        updateJobUI(data);
        if (["ready", "error", "cancelled"].includes(data.status)) {
          stopJobTracking();
        }
      } catch {
        // Retry next tick
      }
    }, 1200);
  }

  function triggerBrowserDownload(url, filename) {
    const a = document.createElement("a");
    a.href = url;
    if (filename) a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 500);
  }

  function updateJobUI(job) {
    const pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
    els.progressFill.style.width = `${pct}%`;
    els.progressTrack.setAttribute("aria-valuenow", String(pct));
    els.jobPercent.textContent = `${pct}%`;
    els.jobStage.textContent = job.stage || "Processing";

    els.jobSpeed.textContent = job.speed ? `Speed: ${job.speed}` : "";
    els.jobEta.textContent = job.eta ? `ETA: ${job.eta}` : "";

    if (job.bpm) {
      els.jobBpm.textContent = `Beat Sync: ${Number(job.bpm).toFixed(0)} BPM`;
      els.jobBpm.classList.remove("hidden");
    }

    if (job.status === "ready") {
      els.downloadBtn.disabled = false;
      els.jobStateBadge.className = "badge badge-ready";
      els.jobStateBadge.textContent = job.from_cache ? "Ready · Cached" : "Ready";
      els.progressFill.classList.add("ready");
      els.jobStage.textContent = `${job.filename} (${job.file_size_text})`;
      els.cancelJobBtn.classList.add("hidden");
      els.retryJobBtn.classList.add("hidden");
      els.jobErrorBox.classList.add("hidden");

      els.saveFileBtn.href = job.download_url;
      els.saveFileBtn.download = job.filename || "";
      els.saveFileBtn.classList.remove("hidden");
      els.newDownloadBtn.classList.remove("hidden");

      if (state.autoTriggeredJobId !== job.job_id) {
        state.autoTriggeredJobId = job.job_id;
        triggerBrowserDownload(job.download_url, job.filename);
      }
    } else if (job.status === "error" || job.status === "cancelled") {
      els.downloadBtn.disabled = false;
      els.jobStateBadge.className = "badge badge-error";
      els.jobStateBadge.textContent = job.status === "cancelled" ? "Cancelled" : "Failed";
      els.cancelJobBtn.classList.add("hidden");
      els.saveFileBtn.classList.add("hidden");
      els.retryJobBtn.classList.remove("hidden");
      els.newDownloadBtn.classList.remove("hidden");

      if (job.error) {
        els.jobErrorText.textContent = job.error;
        els.jobErrorBox.classList.remove("hidden");
      }
    } else {
      els.jobStateBadge.className = "badge badge-running";
      els.jobStateBadge.textContent = "Processing";
    }
  }

  async function cancelCurrentJob() {
    if (!state.currentJobId) return;
    els.cancelJobBtn.disabled = true;
    try {
      await fetch(`/api/jobs/${state.currentJobId}/cancel`, { method: "POST" });
    } catch {
      // Ignore
    }
  }

  // ---------------------------------------------------------------------------
  // Event Bindings
  // ---------------------------------------------------------------------------
  function updateClearButton() {
    els.clearBtn.classList.toggle("hidden", !els.urlInput.value.trim());
  }

  els.urlInput.addEventListener("input", () => {
    updateClearButton();
    clearInputError();
  });

  els.clearBtn.addEventListener("click", () => {
    els.urlInput.value = "";
    updateClearButton();
    clearInputError();
    els.urlInput.focus();
  });

  els.pasteBtn.addEventListener("click", async () => {
    clearInputError();
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        els.urlInput.value = text.trim();
        updateClearButton();
        if (YT_REGEX.test(text.trim())) {
          inspectUrl(text.trim());
        }
      }
    } catch {
      showInputError("Clipboard access was blocked by the browser. Paste manually with Ctrl+V or long-press.");
      els.urlInput.focus();
    }
  });

  els.inspectForm.addEventListener("submit", (e) => {
    e.preventDefault();
    inspectUrl(els.urlInput.value);
  });

  els.tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => selectTab(btn.dataset.tab));
  });

  els.downloadBtn.addEventListener("click", startDownload);
  els.cancelJobBtn.addEventListener("click", cancelCurrentJob);
  els.retryJobBtn.addEventListener("click", startDownload);
  els.newDownloadBtn.addEventListener("click", () => {
    els.jobPanel.classList.add("hidden");
    els.mediaPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  els.clearHistoryBtn.addEventListener("click", () => {
    localStorage.removeItem(HISTORY_KEY);
    renderHistory();
  });

  // Init
  checkHealth();
  renderHistory();
  updateClearButton();
})();
