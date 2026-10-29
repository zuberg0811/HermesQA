/* Dùng chung cho mọi trang: nút chuyển sáng/tối (nhớ bằng localStorage, mặc định theo hệ điều hành). */
(function () {
  const KEY = "hqa.theme";
  const root = document.documentElement;
  try { const t = localStorage.getItem(KEY); if (t === "dark" || t === "light") root.dataset.theme = t; } catch (_) {}

  function isDark() {
    if (root.dataset.theme) return root.dataset.theme === "dark";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function toggle() {
    const next = isDark() ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch (_) {}
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".theme-toggle");
    if (b) { e.preventDefault(); toggle(); }
  });
})();
