(() => {
  const key = "sync-server-url";
  if (!window.localStorage.getItem(key)) {
    const sync = new URL(window.location.origin);
    sync.port = sync.protocol === "https:" ? "8443" : "8787";
    window.localStorage.setItem(key, sync.origin);
  }
})();
