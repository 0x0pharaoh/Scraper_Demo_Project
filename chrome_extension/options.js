const input = document.getElementById("backend");
const btn = document.getElementById("save");

chrome.storage.sync.get({ backend: "http://localhost:10000" }, (cfg) => {
  input.value = cfg.backend;
});

btn.addEventListener("click", () => {
  let url = input.value.trim().replace(/\/+$/, "");
  if (!url) return;
  chrome.storage.sync.set({ backend: url }, () => {
    btn.textContent = "Saved ✓";
    setTimeout(() => (btn.textContent = "Save"), 1200);
  });
});
