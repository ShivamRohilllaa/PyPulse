document.addEventListener("change", (event) => {
  if (event.target.matches("[data-submit]")) event.target.form.requestSubmit();
});
document.addEventListener("submit", (event) => {
  const prompt = event.target.dataset.confirm;
  if (prompt && !window.confirm(prompt)) event.preventDefault();
});
const report = document.querySelector("[data-live]");
async function update() {
  if (!report || report.querySelector('[data-done="yes"]')) return;
  if (!document.hidden && !report.contains(document.activeElement)) {
    try {
      const url = new URL(location.href);
      url.searchParams.set("partial", "1");
      const response = await fetch(url, {cache:"no-store"});
      if (response.redirected) { location.reload(); return; }
      if (response.ok) report.innerHTML = await response.text();
    } catch { /* The next poll retries without clearing the last report. */ }
  }
  window.setTimeout(update, report.dataset.live === "run" ? 2000 : 10000);
}
if (report) window.setTimeout(update, report.dataset.live === "run" ? 2000 : 10000);
