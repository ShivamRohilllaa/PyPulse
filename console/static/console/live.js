document.addEventListener("change", event => {
  if (event.target.matches("[data-submit]")) event.target.form.requestSubmit();
});
document.addEventListener("submit", event => {
  const message = event.submitter?.dataset.confirm || event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-copy-report]");
  if (!button) return;
  try {
    const response = await fetch(button.dataset.copyReport);
    if (!response.ok || response.redirected) throw Error();
    await navigator.clipboard.writeText(await response.text());
    button.textContent = "Summary copied";
  } catch {
    window.location.assign(button.dataset.copyReport);
  }
});

const live = document.querySelector("[data-live]");
if (live) {
  let stream, loading = false, dirty = true, timer;
  const done = () => !!live.querySelector('[data-done="yes"]');
  const frequency = live.dataset.live === "run" ? 2000 : 10000;
  const eligible = () => !document.hidden && !done() && !live.contains(document.activeElement);
  function stopStream() {
    if (stream) stream.close();
    stream = null;
  }
  async function update() {
    if (!eligible() || loading || !dirty) return;
    dirty = false;
    loading = true;
    try {
      const url = new URL(location.href);
      url.searchParams.set("partial", "1");
      const response = await fetch(url, {cache:"no-store"});
      if (response.redirected) { location.reload(); return; }
      const html = await response.text();
      if (response.ok && eligible()) live.innerHTML = html;
      else dirty = true;
      if (done()) {stopStream(); clearInterval(timer);}
    } catch { dirty = true; }
    finally { loading = false; }
  }
  function openStream() {
    if (stream || document.hidden || done() || !window.EventSource || !document.body.hasAttribute("data-events")) return;
    stream = new EventSource("/api/events");
    stream.addEventListener("change", () => {dirty = true; update();});
    stream.onerror = () => {
      if (stream?.readyState === EventSource.CLOSED) stopStream();
    };
  }
  if (!done()) {
    openStream();
    timer = setInterval(() => {
      if (!stream || stream.readyState !== EventSource.OPEN) dirty = true;
      update();
    }, frequency);
    live.addEventListener("focusout", () => setTimeout(update, 0));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopStream();
      else {dirty = true; openStream(); update();}
    });
  }
}
