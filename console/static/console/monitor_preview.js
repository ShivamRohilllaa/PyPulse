const editor = document.querySelector("#monitor-editor");
editor.addEventListener("submit", async (event) => {
  const button = event.submitter;
  if (!button?.matches("[data-preview]")) return;
  event.preventDefault();
  const output = document.querySelector("#preview-result");
  button.disabled = true;
  output.textContent = "Sending test request…";
  try {
    const response = await fetch(button.formAction, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      body: new FormData(editor),
    });
    if (response.redirected || !response.headers.get("Content-Type")?.includes("application/json")) {
      throw new Error("Preview unavailable");
    }
    const preview = await response.json();
    output.textContent = [preview.result, preview.http && `HTTP ${preview.http}`,
      preview.ms != null && `${preview.ms} ms`].filter(Boolean).join(" · ");
  } catch {
    output.textContent = "Preview unavailable. Refresh the page and try again.";
  } finally {
    button.disabled = false;
  }
});
