const siteSel = document.getElementById("site");
const queryInp = document.getElementById("query");
const limitInp = document.getElementById("limit");
const goBtn = document.getElementById("go");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");

function setStatus(msg) {
  statusEl.textContent = msg || "";
}
function setResultLink(url) {
  resultEl.innerHTML = url
    ? `<a href="${url}" target="_blank">Download CSV</a>`
    : "";
}

async function getBackend() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ backend: "http://localhost:10000" }, (cfg) => {
      resolve(cfg.backend.replace(/\/+$/, ""));
    });
  });
}

async function loadPlugins() {
  const backend = await getBackend();
  setStatus("Loading plugins...");
  try {
    const res = await fetch(`${backend}/api/plugins`);
    const data = await res.json();
    siteSel.innerHTML = "";
    (data.plugins || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      siteSel.appendChild(opt);
    });
    setStatus("");
  } catch (e) {
    setStatus("Failed to load plugins. Check backend URL in Settings.");
  }
}

goBtn.addEventListener("click", async () => {
  const backend = await getBackend();
  const site = siteSel.value;
  const query = queryInp.value.trim();
  const limit = parseInt(limitInp.value, 10);
  if (!site || !query) {
    setStatus("Please choose a site and enter a query.");
    return;
  }

  setStatus("Running… this can take a bit.");
  setResultLink("");

  goBtn.disabled = true;
  try {
    const res = await fetch(`${backend}/api/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site,
        query,
        limit: Number.isFinite(limit) ? limit : undefined
      })
    });
    const data = await res.json();

    if (!data.success) {
      setStatus(`Error: ${data.error || "Unknown error"}`);
    } else {
      setStatus(`Done. ${data.count || 0} rows.`);
      setResultLink(data.file_url);
    }
  } catch (e) {
    setStatus(`Request failed: ${e.message}`);
  } finally {
    goBtn.disabled = false;
  }
});

loadPlugins();
