(() => {
  "use strict";

  const meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta || !window.fetch) {
    return;
  }
  const csrfToken = meta.content;
  const originalFetch = window.fetch.bind(window);
  const unsafeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

  window.fetch = (input, init = {}) => {
    const request = input instanceof Request ? input : null;
    const url = new URL(request ? request.url : String(input), window.location.href);
    const method = String(init.method || (request && request.method) || "GET").toUpperCase();
    if (url.origin !== window.location.origin || !unsafeMethods.has(method)) {
      return originalFetch(input, init);
    }
    const headers = new Headers(request ? request.headers : undefined);
    new Headers(init.headers || undefined).forEach((value, name) => headers.set(name, value));
    headers.set("X-CSRF-Token", csrfToken);
    return originalFetch(input, {...init, headers});
  };

  document.querySelectorAll("[data-select-on-click]").forEach((element) => {
    element.addEventListener("click", () => element.select());
  });
  document.querySelectorAll("[data-confirm-delete]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm("파일과 CSV 기록을 삭제할까요?")) {
        event.preventDefault();
      }
    });
  });
})();
