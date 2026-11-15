(() => {
  const key = "pypulse.appearance";
  const root = document.documentElement;
  let saved;
  try { saved = localStorage.getItem(key); } catch {}
  function apply(value) {
    root.dataset.theme = value === "light" ? "light" : "dark";
    const control = document.querySelector("[data-theme-toggle]");
    if (control) {
      control.textContent = root.dataset.theme === "light" ? "Use dark mode" : "Use light mode";
    }
    document.querySelector('meta[name="theme-color"]').content =
      root.dataset.theme === "light" ? "#eef4f1" : "#101717";
  }
  apply(saved);
  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme);
    document.querySelector("[data-theme-toggle]").addEventListener("click", () => {
      apply(root.dataset.theme === "light" ? "dark" : "light");
      try { localStorage.setItem(key, root.dataset.theme); } catch {}
    });
  });
})();
